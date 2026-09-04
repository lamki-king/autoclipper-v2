import os
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, File, Header, HTTPException, UploadFile

from app import production

router = APIRouter()


def check_auth(x_api_key: Optional[str]):
    key = os.getenv('AUTOCLIPPER_API_KEY', '')
    if key and x_api_key != key:
        raise HTTPException(401, 'Invalid or missing X-API-Key')


def download_youtube(url: str, output: Path):
    output.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        'yt-dlp',
        '--no-playlist',
        '--js-runtimes', 'deno',
        '--merge-output-format', 'mp4',
        '-f', 'bv*[height<=1080][ext=mp4]+ba[ext=m4a]/b[height<=1080][ext=mp4]/b[height<=1080]',
        '-o', str(output),
        url,
    ]
    try:
        result = subprocess.run(cmd, text=True, capture_output=True, timeout=1800)
    except FileNotFoundError:
        raise RuntimeError('yt-dlp is not installed')
    if result.returncode != 0:
        raise RuntimeError(result.stderr[-4000:] or 'yt-dlp failed')
    if not output.exists() or output.stat().st_size == 0:
        raise RuntimeError('yt-dlp completed without producing a video file')


def run_youtube_job(job_id: str, url: str, max_clips: int, instruction: str):
    job_dir = production.WORK_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    video = job_dir / 'source.mp4'
    try:
        production.JOBS[job_id]['status'] = 'downloading'
        production.JOBS[job_id]['progress'] = 5
        download_youtube(url, video)
        production.JOBS[job_id]['source_url'] = url
        production.JOBS[job_id]['progress'] = 15
        production.legacy.pipeline(job_id, video, None, max_clips, instruction)
    except Exception as exc:
        production.JOBS[job_id].update(status='failed', progress=0, error=str(exc))
        if video.exists():
            try:
                video.unlink()
            except Exception:
                pass


@router.post('/process-youtube')
def process_youtube(
    background_tasks: BackgroundTasks,
    url: str,
    max_clips: int = 5,
    instruction: str = '',
    x_api_key: Optional[str] = Header(None),
):
    check_auth(x_api_key)
    if not url.startswith(('https://www.youtube.com/', 'https://youtube.com/', 'https://m.youtube.com/', 'https://youtu.be/')):
        raise HTTPException(400, 'Only YouTube URLs are accepted')
    max_clips = max(1, min(int(max_clips), 10))
    job_id = str(uuid.uuid4())
    production.JOBS[job_id] = {
        'status': 'queued',
        'progress': 0,
        'clips': [],
        'instruction': instruction,
        'source_url': url,
        'max_clips': max_clips,
    }
    background_tasks.add_task(run_youtube_job, job_id, url, max_clips, instruction)
    return {'job_id': job_id, 'status': 'queued', 'version': production.app.version}
