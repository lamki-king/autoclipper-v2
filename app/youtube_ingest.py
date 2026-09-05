import os
import subprocess
import uuid
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException

from app import production

router = APIRouter()


def check_auth(x_api_key: Optional[str]):
    key = os.getenv('AUTOCLIPPER_API_KEY', '')
    if key and x_api_key != key:
        raise HTTPException(401, 'Invalid or missing X-API-Key')


def _run_ytdlp(args, output: Path):
    cmd = [
        'yt-dlp', '--no-playlist', '--no-warnings',
        '--retries', '3', '--fragment-retries', '3', '--retry-sleep', '3',
        '--merge-output-format', 'mp4', *args, '-o', str(output)
    ]
    result = subprocess.run(cmd, text=True, capture_output=True, timeout=1800)
    if result.returncode == 0 and output.exists() and output.stat().st_size > 0:
        return
    raise RuntimeError(result.stderr[-5000:] or result.stdout[-5000:] or 'yt-dlp failed')


def download_youtube(url: str, output: Path):
    output.parent.mkdir(parents=True, exist_ok=True)
    attempts = [
        [
            '--remote-components', 'ejs:github',
            '--js-runtimes', 'deno',
            '--extractor-args', 'youtube:player_client=mweb,web_safari,web_embedded,tv',
            '--extractor-args', 'youtubepot-bgutilscript:server_home=/opt/bgutil-ytdlp-pot-provider/server',
            '-f', 'bv*[height<=1080]+ba/b[height<=1080]', url,
        ],
        [
            '--remote-components', 'ejs:github',
            '--js-runtimes', 'deno',
            '--extractor-args', 'youtube:player_client=web_safari,web_embedded,tv',
            '--extractor-args', 'youtubepot-bgutilscript:server_home=/opt/bgutil-ytdlp-pot-provider/server',
            '-f', 'bv*[height<=1080]+ba/b[height<=1080]', url,
        ],
        [
            '--remote-components', 'ejs:github',
            '--js-runtimes', 'deno',
            '--extractor-args', 'youtube:player_client=web_embedded,tv',
            '-f', 'b[height<=720]/18', url,
        ],
        [
            '--extractor-args', 'youtube:player_client=tv',
            '-f', 'b[height<=720]/18', url,
        ],
    ]
    errors = []
    for args in attempts:
        try:
            _run_ytdlp(args, output)
            return
        except Exception as exc:
            errors.append(str(exc))
            if output.exists():
                try:
                    output.unlink()
                except Exception:
                    pass
    raise RuntimeError('YouTube download failed after all supported extraction profiles: ' + ' | '.join(errors)[-9000:])


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
        'status': 'queued', 'progress': 0, 'clips': [],
        'instruction': instruction, 'source_url': url, 'max_clips': max_clips,
    }
    background_tasks.add_task(run_youtube_job, job_id, url, max_clips, instruction)
    return {'job_id': job_id, 'status': 'queued', 'version': production.app.version}
