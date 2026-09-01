import json
import os
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Optional

from fastapi import BackgroundTasks, FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from openai import OpenAI
from pydantic import BaseModel

WORK_DIR = Path(os.getenv("WORK_DIR", "./jobs"))
WORK_DIR.mkdir(parents=True, exist_ok=True)
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
TEXT_MODEL = os.getenv("OPENAI_TEXT_MODEL", "gpt-5.6-luna")
TRANSCRIBE_MODEL = os.getenv("OPENAI_TRANSCRIBE_MODEL", "whisper-1")
MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "500"))
client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

app = FastAPI(title="AutoClipper V2", version="2.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
JOBS: dict[str, dict] = {}

class ClipSegment(BaseModel):
    start: float
    end: float
    title: str
    reason: str

def run_cmd(cmd: list[str]) -> None:
    result = subprocess.run(cmd, text=True, capture_output=True)
    if result.returncode:
        raise RuntimeError(result.stderr[-4000:] or "ffmpeg failed")

def transcribe(video_path: Path, job_dir: Path) -> dict:
    if not client:
        raise RuntimeError("OPENAI_API_KEY is not configured")
    audio = job_dir / "audio.mp3"
    run_cmd(["ffmpeg", "-y", "-i", str(video_path), "-vn", "-ac", "1", "-ar", "16000", "-b:a", "32k", str(audio)])
    if audio.stat().st_size > 24 * 1024 * 1024:
        raise RuntimeError("Audio is too large for the transcription request. Use a shorter video.")
    with audio.open("rb") as f:
        result = client.audio.transcriptions.create(model=TRANSCRIBE_MODEL, file=f, response_format="verbose_json", timestamp_granularities=["segment"])
    segments = [{"start": float(s.start), "end": float(s.end), "text": s.text} for s in (getattr(result, "segments", []) or [])]
    if not segments:
        raise RuntimeError("No speech segments were returned")
    return {"text": getattr(result, "text", ""), "segments": segments}

def find_highlights(transcript: dict, max_clips: int) -> list[ClipSegment]:
    if not client:
        raise RuntimeError("OPENAI_API_KEY is not configured")
    lines = [f"[{s['start']:.1f}-{s['end']:.1f}] {s['text'].strip()}" for s in transcript["segments"]]
    prompt = f"""You select viral short-form moments from a timestamped transcript. Return exactly {max_clips} or fewer clips. Each must be 15-45 seconds, must stay inside the transcript, and should have a strong hook, punchline, emotion, useful insight, conflict, surprise, or high-energy moment. Return ONLY JSON in this shape: {{\"clips\":[{{\"start\":12.3,\"end\":35.2,\"title\":\"short title\",\"reason\":\"why it works\"}}]}}\n\nTRANSCRIPT:\n{chr(10).join(lines)}"""
    response = client.responses.create(model=TEXT_MODEL, input=prompt)
    raw = response.output_text.strip().replace("```json", "").replace("```", "").strip()
    data = json.loads(raw)
    clips = []
    for item in data.get("clips", [])[:max_clips]:
        start, end = float(item["start"]), float(item["end"])
        if end > start and 15 <= end - start <= 45:
            clips.append(ClipSegment(start=start, end=end, title=str(item["title"]), reason=str(item["reason"])))
    if not clips:
        raise RuntimeError("AI did not return valid clip segments")
    return clips

def ass_time(seconds: float) -> str:
    h = int(seconds // 3600); m = int((seconds % 3600) // 60); s = seconds % 60
    return f"{h}:{m:02d}:{s:05.2f}"

def build_ass(transcript: dict, start: float, end: float, path: Path) -> None:
    header = """[Script Info]\nScriptType: v4.00+\nPlayResX: 1080\nPlayResY: 1920\n\n[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, Bold, Outline, Shadow, Alignment, MarginV\nStyle: Default,DejaVu Sans,64,&H00FFFFFF,&H00000000,1,4,1,2,160\n\n[Events]\nFormat: Layer, Start, End, Style, Text\n"""
    events = []
    for seg in transcript["segments"]:
        if seg["end"] < start or seg["start"] > end: continue
        a = max(0.0, seg["start"] - start); b = min(end - start, seg["end"] - start)
        text = seg["text"].strip().replace("{", "\\{").replace("}", "\\}").replace(",", "\\,")
        events.append(f"Dialogue: 0,{ass_time(a)},{ass_time(b)},Default,{text}")
    path.write_text(header + "\n".join(events), encoding="utf-8")

def render_clip(source: Path, transcript: dict, clip: ClipSegment, out: Path, watermark: Optional[Path]) -> None:
    ass = out.with_suffix(".ass"); build_ass(transcript, clip.start, clip.end, ass); duration = clip.end - clip.start
    ass_filter = f"ass={ass.as_posix()}"
    if watermark and watermark.exists():
        cmd = ["ffmpeg", "-y", "-ss", str(clip.start), "-t", str(duration), "-i", str(source), "-i", str(watermark), "-filter_complex", f"[0:v]crop=ih*9/16:ih,scale=1080:1920[c];[1:v]scale=220:-1[wm];[c][wm]overlay=W-w-30:30,{ass_filter}[v]", "-map", "[v]", "-map", "0:a?", "-c:v", "libx264", "-preset", "fast", "-crf", "20", "-c:a", "aac", "-b:a", "160k", str(out)]
    else:
        cmd = ["ffmpeg", "-y", "-ss", str(clip.start), "-t", str(duration), "-i", str(source), "-vf", f"crop=ih*9/16:ih,scale=1080:1920,{ass_filter}", "-map", "0:v", "-map", "0:a?", "-c:v", "libx264", "-preset", "fast", "-crf", "20", "-c:a", "aac", "-b:a", "160k", str(out)]
    run_cmd(cmd)

def pipeline(job_id: str, video: Path, watermark: Optional[Path], max_clips: int) -> None:
    try:
        JOBS[job_id]["status"] = "transcribing"; transcript = transcribe(video, video.parent)
        JOBS[job_id]["status"] = "finding_highlights"; clips = find_highlights(transcript, max_clips)
        JOBS[job_id]["status"] = "rendering"; results = []
        for i, clip in enumerate(clips):
            out = video.parent / f"clip_{i}.mp4"; render_clip(video, transcript, clip, out, watermark)
            results.append({"index": i, "title": clip.title, "reason": clip.reason, "start": clip.start, "end": clip.end, "download_url": f"/download/{job_id}/{i}"})
        JOBS[job_id].update(status="done", clips=results)
    except Exception as exc:
        JOBS[job_id].update(status="failed", error=str(exc))

@app.get("/", response_class=HTMLResponse)
def root():
    ui = Path(__file__).resolve().parent.parent / "frontend" / "index.html"
    if ui.exists(): return HTMLResponse(ui.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>AutoClipper V2</h1><p>Frontend not found.</p>", status_code=500)

@app.get("/health")
def health(): return {"status": "healthy", "openai_configured": bool(OPENAI_API_KEY)}

@app.post("/process")
async def process_video(background_tasks: BackgroundTasks, file: UploadFile = File(...), watermark: Optional[UploadFile] = File(None), max_clips: int = 5):
    if not client: raise HTTPException(503, "OPENAI_API_KEY is not configured")
    if not file.filename: raise HTTPException(400, "A video file is required")
    max_clips = max(1, min(max_clips, 10)); job_id = str(uuid.uuid4()); job_dir = WORK_DIR / job_id; job_dir.mkdir(parents=True, exist_ok=True)
    video = job_dir / "source.mp4"
    with video.open("wb") as out: shutil.copyfileobj(file.file, out)
    if video.stat().st_size > MAX_UPLOAD_MB * 1024 * 1024:
        shutil.rmtree(job_dir, ignore_errors=True); raise HTTPException(413, f"Video exceeds {MAX_UPLOAD_MB} MB")
    watermark_path = None
    if watermark:
        watermark_path = job_dir / "watermark.png"
        with watermark_path.open("wb") as out: shutil.copyfileobj(watermark.file, out)
    JOBS[job_id] = {"status": "queued", "clips": []}; background_tasks.add_task(pipeline, job_id, video, watermark_path, max_clips)
    return {"job_id": job_id, "status": "queued"}

@app.get("/status/{job_id}")
def status(job_id: str):
    if job_id not in JOBS: raise HTTPException(404, "job not found")
    return JOBS[job_id]

@app.get("/download/{job_id}/{clip_index}")
def download(job_id: str, clip_index: int):
    path = WORK_DIR / job_id / f"clip_{clip_index}.mp4"
    if not path.exists(): raise HTTPException(404, "clip not found")
    return FileResponse(path, media_type="video/mp4", filename=path.name)
