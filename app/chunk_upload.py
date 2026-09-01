import os
import shutil
import uuid
from pathlib import Path
from fastapi import APIRouter, HTTPException, Request

router = APIRouter(prefix="/upload", tags=["upload"])
UPLOAD_ROOT = Path(os.getenv("WORK_DIR", "./jobs")) / "_uploads"
UPLOAD_ROOT.mkdir(parents=True, exist_ok=True)

@router.post("/init")
async def init_upload(request: Request):
    data = await request.json()
    filename = str(data.get("filename") or "video.mp4")
    size = int(data.get("size") or 0)
    max_mb = int(os.getenv("MAX_UPLOAD_MB", "500"))
    if size <= 0:
        raise HTTPException(400, "Invalid file size")
    if size > max_mb * 1024 * 1024:
        raise HTTPException(413, f"Video exceeds {max_mb} MB")
    upload_id = uuid.uuid4().hex
    folder = UPLOAD_ROOT / upload_id
    folder.mkdir(parents=True)
    (folder / "meta.txt").write_text(f"{filename}\n{size}\n", encoding="utf-8")
    return {"upload_id": upload_id, "chunk_size": 8 * 1024 * 1024}

@router.put("/chunk/{upload_id}/{chunk_index}")
async def upload_chunk(upload_id: str, chunk_index: int, request: Request):
    folder = UPLOAD_ROOT / upload_id
    if not folder.exists() or chunk_index < 0:
        raise HTTPException(404, "Upload session not found")
    path = folder / f"chunk_{chunk_index:06d}"
    with path.open("wb") as out:
        async for part in request.stream():
            out.write(part)
    return {"ok": True, "chunk": chunk_index}

@router.post("/complete/{upload_id}")
async def complete_upload(upload_id: str, request: Request):
    folder = UPLOAD_ROOT / upload_id
    if not folder.exists():
        raise HTTPException(404, "Upload session not found")
    data = await request.json()
    count = int(data.get("chunks") or 0)
    if count <= 0:
        raise HTTPException(400, "No chunks supplied")
    meta = (folder / "meta.txt").read_text(encoding="utf-8").splitlines()
    filename = meta[0] if meta else "video.mp4"
    expected = int(meta[1]) if len(meta) > 1 else 0
    job_id = uuid.uuid4().hex
    job_dir = Path(os.getenv("WORK_DIR", "./jobs")) / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    video = job_dir / "source.mp4"
    written = 0
    with video.open("wb") as out:
        for i in range(count):
            chunk = folder / f"chunk_{i:06d}"
            if not chunk.exists():
                shutil.rmtree(folder, ignore_errors=True)
                shutil.rmtree(job_dir, ignore_errors=True)
                raise HTTPException(400, f"Missing chunk {i}")
            with chunk.open("rb") as inp:
                shutil.copyfileobj(inp, out, length=1024 * 1024)
            written += chunk.stat().st_size
    shutil.rmtree(folder, ignore_errors=True)
    if expected and written != expected:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise HTTPException(400, "Uploaded file size does not match original")
    from app.main import JOBS, pipeline, client
    if not client:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise HTTPException(503, "OPENAI_API_KEY is not configured in Render")
    max_clips = max(1, min(int(data.get("max_clips") or 5), 10))
    instruction = str(data.get("instruction") or "")
    JOBS[job_id] = {"status": "queued", "progress": 0, "clips": [], "instruction": instruction}
    import asyncio
    asyncio.create_task(_run(job_id, video, max_clips, instruction))
    return {"job_id": job_id, "status": "queued", "filename": filename}

async def _run(job_id, video, max_clips, instruction):
    from app.main import pipeline
    await __import__("asyncio").to_thread(pipeline, job_id, video, None, max_clips, instruction)
