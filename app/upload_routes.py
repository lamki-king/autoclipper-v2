import shutil, uuid
from pathlib import Path
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

router = APIRouter()
UPLOAD_DIR = Path('./jobs/_uploads')
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

class InitBody(BaseModel):
    filename: str
    size: int

class CompleteBody(BaseModel):
    chunks: int
    max_clips: int = 5
    instruction: str = ''

@router.post('/upload/init')
async def upload_init(body: InitBody):
    if body.size <= 0 or body.size > 500 * 1024 * 1024:
        raise HTTPException(413, 'Video must be between 1 byte and 500 MB')
    upload_id = uuid.uuid4().hex
    d = UPLOAD_DIR / upload_id
    d.mkdir(parents=True, exist_ok=True)
    (d / 'meta').write_text(f'{body.filename}\n{body.size}', encoding='utf-8')
    return {'upload_id': upload_id, 'chunk_size': 8 * 1024 * 1024}

@router.put('/upload/chunk/{upload_id}/{index}')
async def upload_chunk(upload_id: str, index: int, request: Request):
    if index < 0 or index > 1000:
        raise HTTPException(400, 'Invalid chunk index')
    d = UPLOAD_DIR / upload_id
    if not d.is_dir():
        raise HTTPException(404, 'Upload session not found')
    p = d / f'{index}.part'
    with p.open('wb') as out:
        async for chunk in request.stream():
            out.write(chunk)
    return {'ok': True, 'index': index, 'bytes': p.stat().st_size}

@router.post('/upload/complete/{upload_id}')
async def upload_complete(upload_id: str, body: CompleteBody):
    d = UPLOAD_DIR / upload_id
    meta = d / 'meta'
    if not d.is_dir() or not meta.exists():
        raise HTTPException(404, 'Upload session not found')
    parts = []
    for i in range(body.chunks):
        p = d / f'{i}.part'
        if not p.exists():
            raise HTTPException(400, f'Missing chunk {i}')
        parts.append(p)
    job_id = uuid.uuid4().hex
    job_dir = Path('./jobs') / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    video = job_dir / 'source.mp4'
    with video.open('wb') as out:
        for p in parts:
            with p.open('rb') as src:
                shutil.copyfileobj(src, out)
    return {'job_id': job_id, 'status': 'uploaded'}
