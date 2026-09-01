import importlib.util, json, os, shutil, uuid
from pathlib import Path
from typing import Optional

import boto3
from botocore.exceptions import ClientError
from fastapi import BackgroundTasks, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

# Load the proven AI/FFmpeg engine without loading its FastAPI routes.
_spec = importlib.util.spec_from_file_location('autoclipper_legacy', Path(__file__).with_name('main.py'))
legacy = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(legacy)

WORK_DIR = Path(os.getenv('WORK_DIR', './jobs')); WORK_DIR.mkdir(parents=True, exist_ok=True)
MAX_UPLOAD_MB = int(os.getenv('MAX_UPLOAD_MB', '500'))
PART_SIZE = max(5 * 1024 * 1024, int(os.getenv('R2_PART_SIZE_MB', '8')) * 1024 * 1024)
R2_ACCOUNT_ID = os.getenv('R2_ACCOUNT_ID', '')
R2_ACCESS_KEY_ID = os.getenv('R2_ACCESS_KEY_ID', '')
R2_SECRET_ACCESS_KEY = os.getenv('R2_SECRET_ACCESS_KEY', '')
R2_BUCKET = os.getenv('R2_BUCKET', '')
R2_ENDPOINT = os.getenv('R2_ENDPOINT') or (f'https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com' if R2_ACCOUNT_ID else '')
R2_REGION = 'auto'

s3 = None
if all([R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET, R2_ENDPOINT]):
    s3 = boto3.client('s3', endpoint_url=R2_ENDPOINT, aws_access_key_id=R2_ACCESS_KEY_ID, aws_secret_access_key=R2_SECRET_ACCESS_KEY, region_name=R2_REGION)

JOBS = legacy.JOBS
legacy.WORK_DIR = WORK_DIR
legacy.JOBS = JOBS
app = FastAPI(title='AutoClipper V5', version='5.0.0')
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
    if key and x_api_key != key:
        raise HTTPException(401, 'Invalid or missing X-API-Key')

@app.get('/', response_class=HTMLResponse)
def root():
    ui = Path(__file__).resolve().parent.parent / 'frontend' / 'index.html'
    return HTMLResponse(ui.read_text(encoding='utf-8')) if ui.exists() else HTMLResponse('<h1>AutoClipper V5</h1>', status_code=500)

@app.head('/')
def root_head():
    return HTMLResponse('')

@app.get('/health')
def health():
    return {'status':'healthy','version':'5.0.0','openai_configured':bool(os.getenv('OPENAI_API_KEY')),'storage':'r2' if s3 else 'not_configured','part_size':PART_SIZE,'max_upload_mb':MAX_UPLOAD_MB,'encoder':legacy.VIDEO_ENCODER}

@app.post('/upload/init')
def upload_init(body: UploadInit):
    require_storage()
    if not body.filename or body.size <= 0:
        raise HTTPException(400, 'A valid filename and size are required')
    if body.size > MAX_UPLOAD_MB * 1024 * 1024:
        raise HTTPException(413, f'Video exceeds {MAX_UPLOAD_MB} MB')
    key = f'uploads/{uuid.uuid4().hex}/{Path(body.filename).name}'
    try:
        created = s3.create_multipart_upload(Bucket=R2_BUCKET, Key=key, ContentType=body.content_type or 'video/mp4')
        upload_id = created['UploadId']
        total = (body.size + PART_SIZE - 1) // PART_SIZE
        urls = []
        for n in range(1, total + 1):
            urls.append({'part_number': n, 'url': s3.generate_presigned_url('upload_part', Params={'Bucket':R2_BUCKET,'Key':key,'UploadId':upload_id,'PartNumber':n}, ExpiresIn=3600)})
        return {'upload_id':upload_id,'key':key,'part_size':PART_SIZE,'parts':urls,'total_parts':total,'status':'initialized'}
    except Exception as e:
        raise HTTPException(502, f'Could not initialize persistent upload: {e}')

@app.post('/upload/complete')
def upload_complete(body: UploadComplete, background_tasks: BackgroundTasks):
    require_storage()
    if body.size > MAX_UPLOAD_MB * 1024 * 1024:
        raise HTTPException(413, 'Video exceeds upload limit')
    parts = sorted(body.parts, key=lambda p: int(p.get('PartNumber', 0)))
    if not parts or [int(p.get('PartNumber',0)) for p in parts] != list(range(1, len(parts)+1)):
        raise HTTPException(409, 'Uploaded parts are incomplete or out of order')
    try:
        s3.complete_multipart_upload(Bucket=R2_BUCKET, Key=body.key, UploadId=body.upload_id, MultipartUpload={'Parts':[{'PartNumber':int(p['PartNumber']),'ETag':p['ETag']} for p in parts]})
        job_id = str(uuid.uuid4()); job_dir = WORK_DIR / job_id; job_dir.mkdir(parents=True, exist_ok=True); video = job_dir / 'source.mp4'
        with video.open('wb') as out:
            s3.download_fileobj(R2_BUCKET, body.key, out)
        if video.stat().st_size != body.size:
            raise RuntimeError(f'Persistent object size mismatch: {video.stat().st_size} != {body.size}')
        JOBS[job_id] = {'status':'queued','progress':0,'clips':[],'instruction':body.instruction,'storage_key':body.key}
        max_clips = max(1, min(int(body.max_clips), 10))
        background_tasks.add_task(legacy.pipeline, job_id, video, None, max_clips, body.instruction)
        return {'job_id':job_id,'status':'queued','version':'5.0.0'}
    except ClientError as e:
        raise HTTPException(502, f'Persistent upload completion failed: {e}')
    except Exception as e:
        raise HTTPException(400, f'Failed to start processing: {e}')

@app.post('/upload/abort')
def upload_abort(key: str, upload_id: str):
    require_storage()
    try:
        s3.abort_multipart_upload(Bucket=R2_BUCKET, Key=key, UploadId=upload_id)
        return {'status':'aborted'}
    except Exception as e:
        raise HTTPException(400, f'Could not abort upload: {e}')

@app.post('/process')
async def process_proxy(background_tasks: BackgroundTasks, x_api_key: Optional[str] = Header(None), **kwargs):
    # Legacy endpoint remains available for compatibility; the production UI uses R2 multipart upload.
    check_auth(x_api_key)
    raise HTTPException(410, 'Direct /process upload is disabled in V5. Use the persistent multipart uploader.')

@app.get('/status/{job_id}')
def status(job_id: str):
    if job_id not in JOBS: raise HTTPException(404, 'job not found')
    return JOBS[job_id]

@app.get('/download/{job_id}/{clip_index}')
def download(job_id: str, clip_index: int):
    path = WORK_DIR / job_id / f'clip_{clip_index}.mp4'
    if not path.exists(): raise HTTPException(404, 'clip not found')
    return legacy.FileResponse(path, media_type='video/mp4', filename=path.name)
