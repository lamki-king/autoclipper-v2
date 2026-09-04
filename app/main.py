import json, os, shutil, subprocess, time, uuid, threading
import boto3
from botocore.exceptions import ClientError
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional
from fastapi import BackgroundTasks, FastAPI, File, Header, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from openai import OpenAI
from pydantic import BaseModel
WORK_DIR=Path(os.getenv('WORK_DIR','./jobs')); WORK_DIR.mkdir(parents=True,exist_ok=True)
UPLOAD_DIR=Path(os.getenv('UPLOAD_DIR',str(WORK_DIR/'_uploads'))); UPLOAD_DIR.mkdir(parents=True,exist_ok=True)
CHUNK_SIZE=max(1024*1024,min(int(os.getenv('UPLOAD_CHUNK_MB','4'))*1024*1024,16*1024*1024))
OPENAI_API_KEY=os.getenv('OPENAI_API_KEY',''); TEXT_MODEL=os.getenv('OPENAI_TEXT_MODEL','gpt-4o-mini'); TRANSCRIBE_MODEL=os.getenv('OPENAI_TRANSCRIBE_MODEL','whisper-1'); MAX_UPLOAD_MB=int(os.getenv('MAX_UPLOAD_MB','500'))
RENDER_WORKERS=max(1,min(int(os.getenv('AUTOCLIPPER_RENDER_WORKERS','2')),4)); ENCODER=os.getenv('AUTOCLIPPER_ENCODER','auto'); API_KEY=os.getenv('AUTOCLIPPER_API_KEY',''); JOB_TTL_HOURS=int(os.getenv('JOB_TTL_HOURS','24')); UPLOAD_TTL_HOURS=int(os.getenv('UPLOAD_TTL_HOURS','6'))
from app.job_store import PersistentJobs

class ClipSegment(BaseModel):
    start:float; end:float; title:str; reason:str; hook:str; category:str; score:int; scores:dict
class UploadInit(BaseModel):
    filename:str; size:int
class UploadComplete(BaseModel):
    chunks:int; max_clips:int=5; instruction:str=''
def run_cmd(cmd):
    r=subprocess.run(cmd,text=True,capture_output=True)
    if r.returncode: raise RuntimeError(r.stderr[-4000:] or 'ffmpeg failed')
    return r.stdout
def choose_encoder():
    if ENCODER!='auto': return ENCODER
    try:
        enc=run_cmd(['ffmpeg','-hide_banner','-encoders']).lower()
        if 'h264_nvenc' in enc and shutil.which('nvidia-smi'): return 'h264_nvenc'
    except Exception: pass
    return 'libx264'
VIDEO_ENCODER=choose_encoder()
def cleanup_old_jobs():
    cutoff=time.time()-JOB_TTL_HOURS*3600
    for d in WORK_DIR.iterdir():
        try:
            if d.is_dir() and d.name!='_uploads' and d.stat().st_mtime<cutoff: shutil.rmtree(d,ignore_errors=True); JOBS.pop(d.name,None)
        except Exception: pass
def cleanup_old_uploads():
    cutoff=time.time()-UPLOAD_TTL_HOURS*3600
    for d in UPLOAD_DIR.iterdir():
        try:
            if d.is_dir() and d.stat().st_mtime<cutoff: shutil.rmtree(d,ignore_errors=True)
        except Exception: pass
def transcribe(video_path,job_dir):
    if not client: raise RuntimeError('OPENAI_API_KEY is not configured in Render')
    audio=job_dir/'audio.mp3'; run_cmd(['ffmpeg','-y','-threads','0','-i',str(video_path),'-vn','-ac','1','-ar','16000','-b:a','24k',str(audio)])
    if audio.stat().st_size>24*1024*1024: raise RuntimeError('Audio is too large for transcription. Use a shorter video.')
    with audio.open('rb') as f: result=client.audio.transcriptions.create(model=TRANSCRIBE_MODEL,file=f,response_format='verbose_json',timestamp_granularities=['segment'])
    segs=[{'start':float(s.start),'end':float(s.end),'text':s.text.strip()} for s in (getattr(result,'segments',[]) or [])]
    if not segs: raise RuntimeError('No speech segments were returned')
    return {'text':getattr(result,'text',''),'segments':segs}
def find_highlights(transcript,max_clips,instruction=''):
    if not client: raise RuntimeError('OPENAI_API_KEY is not configured in Render')
    lines='\n'.join(f"[{s['start']:.1f}-{s['end']:.1f}] {s['text']}" for s in transcript['segments'])
    prompt=f'''You are AutoClipper's elite short-form editorial engine. Select up to {max_clips} distinct, standalone moments. Creator instruction: {instruction.strip() or 'Find the strongest viral moments.'}
Rules: 15-60 seconds; never cut mid-thought; start on the hook or necessary setup and end after payoff; favor surprise, emotion, humor, conflict, useful advice, memorable quotes and high information density; reject greetings, weak setup, repetition and context-dependent fragments; avoid overlapping clips unless the moments are genuinely distinct. Optimize for retention and comprehension, not just sensationalism. Score 0-99 for hook, flow, value, emotion, shareability and clarity. category: story, insight, controversy, humor, emotion, tutorial, reaction, motivation, news, other. hook is a short headline and must not fabricate a quote. Return ONLY JSON: {{"clips":[{{"start":12.3,"end":38.5,"title":"Short title","reason":"Why it works","hook":"Scroll-stopping headline","category":"insight","score":94,"scores":{{"hook":96,"flow":92,"value":95,"emotion":88,"shareability":94,"clarity":96}}}}]}}
TRANSCRIPT:\n{lines}'''
    r=client.chat.completions.create(model=TEXT_MODEL,messages=[{'role':'user','content':prompt}],response_format={'type':'json_object'}); raw=(r.choices[0].message.content or '').strip().replace('```json','').replace('```','').strip(); data=json.loads(raw); clips=[]
    for x in data.get('clips',[])[:max_clips]:
        try: start,end=float(x['start']),float(x['end'])
        except Exception: continue
        if not(end>start and 15<=end-start<=60): continue
        scores={k:max(0,min(99,int(v))) for k,v in dict(x.get('scores',{})).items() if k in {'hook','flow','value','emotion','shareability','clarity'}}; score=max(0,min(99,int(x.get('score',0)))) or (round(sum(scores.values())/len(scores)) if scores else 0)
        clips.append(ClipSegment(start=start,end=end,title=str(x.get('title','Untitled clip')),reason=str(x.get('reason','Strong short-form moment.')),hook=str(x.get('hook',x.get('title',''))),category=str(x.get('category','other')),score=score,scores=scores))
    clips.sort(key=lambda c:c.score,reverse=True); selected=[]
    for c in clips:
        if all(c.end<=p.start+2 or c.start>=p.end-2 for p in selected): selected.append(c)
    if not selected: raise RuntimeError('AI did not return valid distinct clip segments')
    return selected
def ass_time(s): return f"{int(s//3600)}:{int(s%3600//60):02d}:{s%60:05.2f}"
def build_ass(transcript,start,end,path):
    h='''[Script Info]\nScriptType: v4.00+\nPlayResX: 1080\nPlayResY: 1920\n\n[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, Bold, Outline, Shadow, Alignment, MarginV\nStyle: Default,DejaVu Sans,64,&H00FFFFFF,&H00000000,1,4,1,2,160\n\n[Events]\nFormat: Layer, Start, End, Style, Text\n'''; events=[]
    for s in transcript['segments']:
        if s['end']<start or s['start']>end: continue
        a=max(0,s['start']-start); b=min(end-start,s['end']-start); t=s['text'].strip().replace('{','\\{').replace('}','\\}').replace(',','\\,'); events.append(f'Dialogue: 0,{ass_time(a)},{ass_time(b)},Default,{t}')
    path.write_text(h+'\n'.join(events),encoding='utf-8')
def render_clip(source,transcript,clip,out,watermark):
    ass=out.with_suffix('.ass'); build_ass(transcript,clip.start,clip.end,ass); dur=clip.end-clip.start; af=f'ass={ass.as_posix()}'; base='scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920'; codec=['-c:v',VIDEO_ENCODER]
    if VIDEO_ENCODER=='h264_nvenc': codec+=['-preset','p1','-cq','21']
    else: codec+=['-preset','veryfast','-crf','21','-threads','0']
    if watermark and watermark.exists():
        fc=f'[0:v]{base}[c];[1:v]scale=220:-1[wm];[c][wm]overlay=W-w-30:30,{af}[v]'; cmd=['ffmpeg','-y','-threads','0','-ss',str(clip.start),'-t',str(dur),'-i',str(source),'-i',str(watermark),'-filter_complex',fc,'-map','[v]','-map','0:a?']+codec+['-c:a','aac','-b:a','128k','-movflags','+faststart','-loglevel','error',str(out)]
    else: cmd=['ffmpeg','-y','-threads','0','-ss',str(clip.start),'-t',str(dur),'-i',str(source),'-vf',f'{base},{af}','-map','0:v','-map','0:a?']+codec+['-c:a','aac','-b:a','128k','-movflags','+faststart','-loglevel','error',str(out)]
    run_cmd(cmd); ass.unlink(missing_ok=True)
def social_package(clips,instruction):
    if not client: return {}
    brief='\n'.join(f'{i+1}. {c.title} | {c.hook} | {c.category} | {c.score}' for i,c in enumerate(clips))
    try:
        r=client.chat.completions.create(model=TEXT_MODEL,messages=[{'role':'user','content':f'''Create concise social metadata for these clips. Instruction: {instruction or 'maximize clarity and shareability'}. Return ONLY JSON with keys youtube_shorts,tiktok,instagram. Each value has title,caption,hashtags(array). Do not invent facts.\n{brief}'''}],response_format={'type':'json_object'}); raw=(r.choices[0].message.content or '').strip().replace('```json','').replace('```','').strip(); return json.loads(raw)
    except Exception: return {}
def render_one(args):
    job_id,video,transcript,clip,i,wm=args; out=video.parent/f'clip_{i}.mp4'; render_clip(video,transcript,clip,out,wm); return {'index':i,**clip.model_dump(),'download_url':f'/download/{job_id}/{i}'}
def pipeline(job_id,video,watermark,max_clips,instruction):
    try:
        JOBS[job_id]['status']='transcribing'; JOBS[job_id]['progress']=10; transcript=transcribe(video,video.parent); JOBS[job_id]['status']='scoring_highlights'; JOBS[job_id]['progress']=35; clips=find_highlights(transcript,max_clips,instruction); JOBS[job_id].update(status='rendering',progress=40,total_clips=len(clips),completed_clips=0,render_workers=min(RENDER_WORKERS,len(clips)),encoder=VIDEO_ENCODER); args=[(job_id,video,transcript,c,i,watermark) for i,c in enumerate(clips)]; results=[None]*len(args)
        with ThreadPoolExecutor(max_workers=min(RENDER_WORKERS,len(args))) as pool:
            futures={pool.submit(render_one,a):i for i,a in enumerate(args)}; completed=0
            for f in as_completed(futures):
                i=futures[f]
                try: results[i]=f.result()
                except Exception as e: results[i]={'index':i,'error':str(e)}
                completed+=1; JOBS[job_id]['completed_clips']=completed; JOBS[job_id]['progress']=40+int(completed/len(args)*50)
        JOBS[job_id]['status']='creating_social_package'; JOBS[job_id]['progress']=95; JOBS[job_id].update(status='done',progress=100,clips=results,social=social_package(clips,instruction))
    except Exception as e: JOBS[job_id].update(status='failed',error=str(e))
def check_auth(x_api_key:Optional[str]):
    if API_KEY and x_api_key!=API_KEY: raise HTTPException(401,'Invalid or missing X-API-Key')
@app.get('/',response_class=HTMLResponse)
def root():
    ui=Path(__file__).resolve().parent.parent/'frontend'/'index.html'; return HTMLResponse(ui.read_text(encoding='utf-8')) if ui.exists() else HTMLResponse('<h1>AutoClipper V4</h1><p>Frontend not found.</p>',status_code=500)
@app.head('/')
def root_head(): return HTMLResponse('')
@app.get('/health')
def health(): return {'status':'healthy','version':'4.1.1','openai_configured':bool(OPENAI_API_KEY),'text_model':TEXT_MODEL,'transcribe_model':TRANSCRIBE_MODEL,'render_workers':RENDER_WORKERS,'encoder':VIDEO_ENCODER,'chunk_size':CHUNK_SIZE,'auth_required':bool(API_KEY)}
@app.post('/upload/init')
def upload_init(body:UploadInit):
    cleanup_old_uploads()
    if not body.filename: raise HTTPException(400,'filename is required')
    if body.size<=0: raise HTTPException(400,'file size is required')
    if body.size>MAX_UPLOAD_MB*1024*1024: raise HTTPException(413,f'Video exceeds {MAX_UPLOAD_MB} MB')
    upload_id=uuid.uuid4().hex; directory=UPLOAD_DIR/upload_id; directory.mkdir(parents=True,exist_ok=False); (directory/'meta.json').write_text(json.dumps({'filename':Path(body.filename).name,'size':body.size,'chunk_size':CHUNK_SIZE}),encoding='utf-8'); return {'upload_id':upload_id,'chunk_size':CHUNK_SIZE,'status':'initialized'}
@app.post('/upload/chunk/{upload_id}/{chunk_index}')
async def upload_chunk(upload_id:str,chunk_index:int,chunk:UploadFile=File(...)):
    if chunk_index<0: raise HTTPException(400,'Invalid chunk index')
    directory=UPLOAD_DIR/upload_id; meta_path=directory/'meta.json'
    if not directory.is_dir() or not meta_path.exists(): raise HTTPException(404,'Upload session not found')
    try: meta=json.loads(meta_path.read_text(encoding='utf-8')); max_chunk=int(meta['chunk_size'])
    except Exception: raise HTTPException(500,'Upload session metadata is corrupt')
    target=directory/f'{chunk_index:08d}.part'; temp=directory/f'.{chunk_index:08d}.uploading'
    if target.exists(): return {'upload_id':upload_id,'chunk_index':chunk_index,'received':target.stat().st_size,'status':'already_uploaded'}
    received=0
    try:
        with temp.open('wb') as out:
            while True:
                data=await chunk.read(1024*1024)
                if not data: break
                received+=len(data)
                if received>max_chunk: raise HTTPException(413,f'Chunk exceeds {max_chunk} bytes')
                out.write(data)
        os.replace(temp,target)
    except HTTPException:
        temp.unlink(missing_ok=True); raise
    except Exception as e:
        temp.unlink(missing_ok=True); raise HTTPException(400,f'Failed to save chunk: {e}')
    return {'upload_id':upload_id,'chunk_index':chunk_index,'received':received,'status':'uploaded'}
@app.post('/upload/complete/{upload_id}')
def upload_complete(upload_id:str,body:UploadComplete,background_tasks:BackgroundTasks):
    cleanup_old_uploads(); directory=UPLOAD_DIR/upload_id; meta_path=directory/'meta.json'
    if not directory.is_dir() or not meta_path.exists(): raise HTTPException(404,'Upload session not found')
    if body.chunks<1 or body.chunks>(MAX_UPLOAD_MB*1024*1024//CHUNK_SIZE)+1: raise HTTPException(400,'Invalid chunk count')
    meta=json.loads(meta_path.read_text(encoding='utf-8')); expected_size=int(meta['size']); parts=[directory/f'{i:08d}.part' for i in range(body.chunks)]; missing=[i for i,p in enumerate(parts) if not p.exists()]
    if missing: raise HTTPException(409,f'Missing chunks: {missing[:10]}')
    total=sum(p.stat().st_size for p in parts)
    if total!=expected_size: raise HTTPException(409,f'Upload size mismatch: received {total}, expected {expected_size}')
    job_id=str(uuid.uuid4()); job_dir=WORK_DIR/job_id; job_dir.mkdir(parents=True,exist_ok=True); video=job_dir/'source.mp4'
    try:
        with video.open('wb') as out:
            for p in parts:
                with p.open('rb') as src: shutil.copyfileobj(src,out,length=1024*1024)
        if video.stat().st_size!=expected_size: raise RuntimeError('Assembled video size mismatch')
    except Exception as e:
        shutil.rmtree(job_dir,ignore_errors=True); raise HTTPException(400,f'Failed to assemble upload: {e}')
    shutil.rmtree(directory,ignore_errors=True); max_clips=max(1,min(int(body.max_clips),10)); JOBS[job_id]={'status':'queued','progress':0,'clips':[],'instruction':body.instruction}; background_tasks.add_task(pipeline,job_id,video,None,max_clips,body.instruction); return {'job_id':job_id,'status':'queued','version':'4.1.1'}
@app.post('/process')
async def process_video(background_tasks:BackgroundTasks,file:UploadFile=File(...),watermark:Optional[UploadFile]=File(None),max_clips:int=5,instruction:str='',x_api_key:Optional[str]=Header(None)):
    check_auth(x_api_key); cleanup_old_jobs()
    if not client: raise HTTPException(503,'OPENAI_API_KEY is not configured in Render')
    if not file.filename: raise HTTPException(400,'A video file is required')
    max_clips=max(1,min(max_clips,10)); job_id=str(uuid.uuid4()); job_dir=WORK_DIR/job_id; job_dir.mkdir(parents=True,exist_ok=True); video=job_dir/'source.mp4'; max_bytes=MAX_UPLOAD_MB*1024*1024; size=0
    try:
        with video.open('wb') as out:
            while True:
                chunk=await file.read(1024*1024)
                if not chunk: break
                size+=len(chunk)
                if size>max_bytes: raise HTTPException(413,f'Video exceeds {MAX_UPLOAD_MB} MB')
                out.write(chunk)
    except HTTPException: shutil.rmtree(job_dir,ignore_errors=True); raise
    except Exception: shutil.rmtree(job_dir,ignore_errors=True); raise HTTPException(400,'Failed to save uploaded video')
    wm=None
    if watermark:
        wm=job_dir/'watermark.png'
        with wm.open('wb') as out: shutil.copyfileobj(watermark.file,out)
    JOBS[job_id]={'status':'queued','progress':0,'clips':[],'instruction':instruction}; background_tasks.add_task(pipeline,job_id,video,wm,max_clips,instruction); return {'job_id':job_id,'status':'queued','version':'4.1.1'}
@app.get('/status/{job_id}')
def status(job_id:str):
    if job_id not in JOBS: raise HTTPException(404,'job not found')
    return JOBS[job_id]
@app.get('/download/{job_id}/{clip_index}')
def download(job_id:str,clip_index:int):
    path=WORK_DIR/job_id/f'clip_{clip_index}.mp4'
    if not path.exists(): raise HTTPException(404,'clip not found')
    return FileResponse(path,media_type='video/mp4',filename=path.name)
