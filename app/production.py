import json
import urllib.request
import importlib.util, os, shutil, uuid
from pathlib import Path
from typing import Optional

from fastapi import BackgroundTasks, FastAPI, File, Header, HTTPException, UploadFile, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel

_spec = importlib.util.spec_from_file_location('autoclipper_legacy', Path(__file__).with_name('main.py'))
legacy = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(legacy)

WORK_DIR = Path(os.getenv('WORK_DIR', './jobs')); WORK_DIR.mkdir(parents=True, exist_ok=True)
MAX_UPLOAD_MB = int(os.getenv('MAX_UPLOAD_MB', '500'))
PART_SIZE = max(5 * 1024 * 1024, int(os.getenv('UPLOAD_PART_SIZE_MB', '8')) * 1024 * 1024)

JOBS = legacy.JOBS
legacy.WORK_DIR = WORK_DIR
legacy.JOBS = JOBS
app = FastAPI(title='AutoClipper V5', version='5.1.0')
app.add_middleware(CORSMiddleware, allow_origins=['*'], allow_methods=['*'], allow_headers=['*'])

class UploadInit(BaseModel):
    filename: str
    size: int
    content_type: str = 'video/mp4'

class UploadComplete(BaseModel):
    key: str
    upload_id: str
    parts: list[dict]
    filename: str
    size: int
    max_clips: int = 5
    instruction: str = ''

def require_storage():
    if not s3:
        raise HTTPException(503, 'Persistent upload storage is not configured. Set R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY and R2_BUCKET in Render.')

def check_auth(x_api_key: Optional[str]):
    key = os.getenv('AUTOCLIPPER_API_KEY', '')
    if key and x_api_key != key: raise HTTPException(401, 'Invalid or missing X-API-Key')

@app.get('/', response_class=HTMLResponse)
def root():
    ui = Path(__file__).resolve().parent.parent / 'frontend' / 'index.html'
    return HTMLResponse(ui.read_text(encoding='utf-8')) if ui.exists() else HTMLResponse('<h1>AutoClipper V5</h1>', status_code=500)

@app.head('/')
def root_head(): return HTMLResponse('')

@app.get('/health')
def health():
    return {'status':'healthy','version':'5.1.0','gemini_configured':bool(os.getenv('GEMINI_API_KEY')),'storage':'local_uploads','part_size':PART_SIZE,'max_upload_mb':MAX_UPLOAD_MB,'encoder':legacy.VIDEO_ENCODER}

@app.post('/upload/init')
def upload_init(body: UploadInit):
    if not body.filename or body.size <= 0:
        raise HTTPException(400, 'A valid filename and size are required')
    if body.size > MAX_UPLOAD_MB * 1024 * 1024:
        raise HTTPException(413, f'Video exceeds {MAX_UPLOAD_MB} MB')
    upload_id = uuid.uuid4().hex
    directory = WORK_DIR / '_uploads' / upload_id
    directory.mkdir(parents=True, exist_ok=False)
    meta = {'filename': Path(body.filename).name, 'size': body.size, 'content_type': body.content_type or 'video/mp4'}
    (directory / 'meta.json').write_text(json.dumps(meta), encoding='utf-8')
    return {'upload_id': upload_id, 'key': f'uploads/{upload_id}/{meta["filename"]}', 'part_size': PART_SIZE, 'total_parts': (body.size + PART_SIZE - 1) // PART_SIZE, 'status': 'initialized'}

@app.put('/upload/raw-part/{upload_id}/{part_number}')
async def upload_raw_part(upload_id: str, part_number: int, key: str, request: Request):
    if part_number < 1 or part_number > 10000:
        raise HTTPException(400, 'Invalid part number')
    directory = WORK_DIR / '_uploads' / upload_id
    if not directory.is_dir() or not key.startswith(f'uploads/{upload_id}/'):
        raise HTTPException(404, 'Upload session not found')
    body = await request.body()
    if not body or len(body) > PART_SIZE + 1024 * 1024:
        raise HTTPException(413, 'Invalid chunk size')
    part = directory / f'{part_number:08d}.part'
    part.write_bytes(body)
    import hashlib
    etag = hashlib.md5(body).hexdigest()
    return {'PartNumber': part_number, 'ETag': etag, 'upload_id': upload_id, 'received': len(body)}

@app.post('/upload/complete')
def upload_complete(body: UploadComplete, background_tasks: BackgroundTasks):
    max_bytes = MAX_UPLOAD_MB * 1024 * 1024
    if body.size <= 0 or body.size > max_bytes:
        raise HTTPException(413, 'Invalid video size')
    directory = WORK_DIR / '_uploads' / body.upload_id
    meta_path = directory / 'meta.json'
    if not directory.is_dir() or not meta_path.exists():
        raise HTTPException(404, 'Upload session not found')
    expected = (body.size + PART_SIZE - 1) // PART_SIZE
    parts = sorted(body.parts, key=lambda p: int(p.get('PartNumber', 0)))
    numbers = [int(p.get('PartNumber', 0)) for p in parts]
    if numbers != list(range(1, expected + 1)):
        raise HTTPException(409, f'All {expected} upload parts are required')
    job_id = str(uuid.uuid4())
    job_dir = WORK_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    video = job_dir / 'source.mp4'
    try:
        with video.open('wb') as out:
            for n in numbers:
                part = directory / f'{n:08d}.part'
                if not part.exists():
                    raise HTTPException(409, f'Missing upload part {n}')
                with part.open('rb') as src:
                    shutil.copyfileobj(src, out, length=1024 * 1024)
        if video.stat().st_size != body.size:
            raise RuntimeError(f'Upload size mismatch: {video.stat().st_size} != {body.size}')
        max_clips = max(1, min(int(body.max_clips), 10))
        JOBS[job_id] = {'status': 'queued', 'progress': 0, 'clips': [], 'instruction': body.instruction, 'storage_key': body.key}
        background_tasks.add_task(legacy.pipeline, job_id, video, None, max_clips, body.instruction)
        shutil.rmtree(directory, ignore_errors=True)
        return {'job_id': job_id, 'status': 'queued', 'version': app.version}
    except HTTPException:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise
    except Exception as e:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise HTTPException(400, f'Failed to start processing: {e}')

@app.post('/upload/abort')
def upload_abort(key: str, upload_id: str):
    shutil.rmtree(WORK_DIR / '_uploads' / upload_id, ignore_errors=True)
    return {'status': 'aborted'}

@app.post('/process-raw')
async def process_raw(request: Request, filename: str = 'source.mp4', source_url: str = '', max_clips: int = 5, instruction: str = '', x_api_key: Optional[str] = Header(None)):
    check_auth(x_api_key); legacy.cleanup_old_jobs()
    if not legacy.client: raise HTTPException(503, 'GEMINI_API_KEY is not configured in Render')
    max_clips = max(1, min(int(max_clips), 10)); safe_name = Path(filename).name or 'source.mp4'; job_id = str(uuid.uuid4()); job_dir = WORK_DIR / job_id; job_dir.mkdir(parents=True, exist_ok=True); video = job_dir / 'source.mp4'; max_bytes = MAX_UPLOAD_MB * 1024 * 1024; size = 0
    try:
        with video.open('wb') as out:
            async for chunk in request.stream():
                if chunk:
                    size += len(chunk)
                    if size > max_bytes: raise HTTPException(413, f'Video exceeds {MAX_UPLOAD_MB} MB')
                    out.write(chunk)
    except HTTPException:
        shutil.rmtree(job_dir, ignore_errors=True); raise
    except Exception as e:
        shutil.rmtree(job_dir, ignore_errors=True); raise HTTPException(400, f'Failed to save raw video upload: {e}')
    if size <= 0:
        shutil.rmtree(job_dir, ignore_errors=True); raise HTTPException(400, 'Empty video upload')
    JOBS[job_id] = {'status':'queued','progress':0,'clips':[],'instruction':instruction,'source_url':source_url,'source_filename':safe_name,'max_clips':max_clips}
    background_tasks = BackgroundTasks(); background_tasks.add_task(legacy.pipeline, job_id, video, None, max_clips, instruction)
    response = JSONResponse({'job_id':job_id,'status':'queued','version':app.version}); response.background = background_tasks; return response

@app.post('/process-remote')
async def process_remote(body: dict, background_tasks: BackgroundTasks, x_api_key: Optional[str] = Header(None)):
    check_auth(x_api_key); legacy.cleanup_old_jobs()
    if not legacy.client:
        raise HTTPException(503, 'GEMINI_API_KEY is not configured in Render')
    source_url = str(body.get('url') or '').strip()
    if not source_url:
        raise HTTPException(400, 'A Dropbox temporary URL is required')
    max_clips = max(1, min(int(body.get('max_clips', 5)), 10))
    instruction = str(body.get('instruction') or '')
    filename = Path(str(body.get('filename') or 'source.mp4')).name or 'source.mp4'
    job_id = str(uuid.uuid4())
    job_dir = WORK_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    video = job_dir / 'source.mp4'
    max_bytes = MAX_UPLOAD_MB * 1024 * 1024
    size = 0
    try:
        req = urllib.request.Request(source_url, headers={'User-Agent': 'AutoClipper/5.2'})
        with urllib.request.urlopen(req, timeout=60) as src, video.open('wb') as out:
            length = src.headers.get('Content-Length')
            if length and int(length) > max_bytes:
                raise HTTPException(413, f'Video exceeds {MAX_UPLOAD_MB} MB')
            while True:
                chunk = src.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > max_bytes:
                    raise HTTPException(413, f'Video exceeds {MAX_UPLOAD_MB} MB')
                out.write(chunk)
    except HTTPException:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise
    except Exception as e:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise HTTPException(400, f'Failed to fetch remote video: {e}')
    if size <= 0:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise HTTPException(400, 'Remote video was empty')
    JOBS[job_id] = {'status':'queued','progress':0,'clips':[],'instruction':instruction,'source_url':str(body.get('source_url') or source_url),'source_filename':filename,'max_clips':max_clips}
    background_tasks.add_task(legacy.pipeline, job_id, video, None, max_clips, instruction)
    return {'job_id':job_id,'status':'queued','version':app.version,'source_filename':filename,'size':size}

@app.get('/status/{job_id}')
def status(job_id: str):
    if job_id not in JOBS: raise HTTPException(404, 'job not found')
    data = dict(JOBS[job_id]); data['job_id'] = job_id; return data

@app.get('/download/{job_id}/{clip_index}')
def download(job_id: str, clip_index: int):
    path = WORK_DIR / job_id / f'clip_{clip_index}.mp4'
    if not path.exists(): raise HTTPException(404, 'clip not found')
    return FileResponse(path, media_type='video/mp4', filename=path.name)
