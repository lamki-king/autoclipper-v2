import json, os, shutil, subprocess, uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional
from fastapi import BackgroundTasks, FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from openai import OpenAI
from pydantic import BaseModel

WORK_DIR=Path(os.getenv('WORK_DIR','./jobs')); WORK_DIR.mkdir(parents=True,exist_ok=True)
OPENAI_API_KEY=os.getenv('OPENAI_API_KEY',''); TEXT_MODEL=os.getenv('OPENAI_TEXT_MODEL','gpt-4o-mini'); TRANSCRIBE_MODEL=os.getenv('OPENAI_TRANSCRIBE_MODEL','whisper-1'); MAX_UPLOAD_MB=int(os.getenv('MAX_UPLOAD_MB','500'))
RENDER_WORKERS=max(1,min(int(os.getenv('AUTOCLIPPER_RENDER_WORKERS','2')),4))
client=OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None
app=FastAPI(title='AutoClipper V3',version='3.1.0'); app.add_middleware(CORSMiddleware,allow_origins=['*'],allow_methods=['*'],allow_headers=['*']); JOBS={}

class ClipSegment(BaseModel):
    start:float; end:float; title:str; reason:str; hook:str; category:str; score:int; scores:dict

def run_cmd(cmd):
    r=subprocess.run(cmd,text=True,capture_output=True)
    if r.returncode: raise RuntimeError(r.stderr[-4000:] or 'ffmpeg failed')

def transcribe(video_path,job_dir):
    if not client: raise RuntimeError('OPENAI_API_KEY is not configured in Render')
    audio=job_dir/'audio.mp3'; run_cmd(['ffmpeg','-y','-i',str(video_path),'-vn','-ac','1','-ar','16000','-b:a','32k',str(audio)])
    if audio.stat().st_size>24*1024*1024: raise RuntimeError('Audio is too large for transcription. Use a shorter video.')
    with audio.open('rb') as f: result=client.audio.transcriptions.create(model=TRANSCRIBE_MODEL,file=f,response_format='verbose_json',timestamp_granularities=['segment'])
    segs=[{'start':float(s.start),'end':float(s.end),'text':s.text.strip()} for s in (getattr(result,'segments',[]) or [])]
    if not segs: raise RuntimeError('No speech segments were returned')
    return {'text':getattr(result,'text',''),'segments':segs}

def find_highlights(transcript,max_clips,instruction=''):
    if not client: raise RuntimeError('OPENAI_API_KEY is not configured in Render')
    lines='\n'.join(f"[{s['start']:.1f}-{s['end']:.1f}] {s['text']}" for s in transcript['segments'])
    prompt=f'''You are a senior short-form editor. Select up to {max_clips} distinct viral moments. Creator instruction: {instruction.strip() or 'Find the strongest viral moments.'}
Rules: clips 15-60 seconds; stay within timestamps; prefer hook-development-payoff; favor surprise, emotion, humor, conflict, useful advice, memorable quotes and high information density; avoid greetings, weak setup and context-dependent fragments. Score 0-99 for hook, flow, value, emotion, shareability and clarity. category: story, insight, controversy, humor, emotion, tutorial, reaction, motivation, news, other. hook is a short headline, not a fabricated quote. Return ONLY JSON: {{"clips":[{{"start":12.3,"end":38.5,"title":"Short title","reason":"Why it works","hook":"Scroll-stopping headline","category":"insight","score":94,"scores":{{"hook":96,"flow":92,"value":95,"emotion":88,"shareability":94,"clarity":96}}}}]}}
TRANSCRIPT:\n{lines}'''
    r=client.responses.create(model=TEXT_MODEL,input=prompt); raw=r.output_text.strip().replace('```json','').replace('```','').strip(); data=json.loads(raw); clips=[]
    for x in data.get('clips',[])[:max_clips]:
        try: start,end=float(x['start']),float(x['end'])
        except: continue
        if not(end>start and 15<=end-start<=60): continue
        scores={k:max(0,min(99,int(v))) for k,v in dict(x.get('scores',{})).items() if k in {'hook','flow','value','emotion','shareability','clarity'}}
        score=max(0,min(99,int(x.get('score',0)))) or (round(sum(scores.values())/len(scores)) if scores else 0)
        clips.append(ClipSegment(start=start,end=end,title=str(x.get('title','Untitled clip')),reason=str(x.get('reason','Strong short-form moment.')),hook=str(x.get('hook',x.get('title',''))),category=str(x.get('category','other')),score=score,scores=scores))
    clips.sort(key=lambda c:c.score,reverse=True)
    if not clips: raise RuntimeError('AI did not return valid clip segments')
    return clips

def ass_time(s): return f"{int(s//3600)}:{int(s%3600//60):02d}:{s%60:05.2f}"

def build_ass(transcript,start,end,path):
    h='''[Script Info]\nScriptType: v4.00+\nPlayResX: 1080\nPlayResY: 1920\n\n[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, Bold, Outline, Shadow, Alignment, MarginV\nStyle: Default,DejaVu Sans,64,&H00FFFFFF,&H00000000,1,4,1,2,160\n\n[Events]\nFormat: Layer, Start, End, Style, Text\n'''; events=[]
    for s in transcript['segments']:
        if s['end']<start or s['start']>end: continue
        a=max(0,s['start']-start); b=min(end-start,s['end']-start); t=s['text'].strip().replace('{','\\{').replace('}','\\}').replace(',','\\,'); events.append(f'Dialogue: 0,{ass_time(a)},{ass_time(b)},Default,{t}')
    path.write_text(h+'\n'.join(events),encoding='utf-8')

def render_clip(source,transcript,clip,out,watermark):
    ass=out.with_suffix('.ass'); build_ass(transcript,clip.start,clip.end,ass); dur=clip.end-clip.start; af=f'ass={ass.as_posix()}'
    if watermark and watermark.exists():
        fc=f'[0:v]crop=ih*9/16:ih,scale=1080:1920[c];[1:v]scale=220:-1[wm];[c][wm]overlay=W-w-30:30,{af}[v]'
        cmd=['ffmpeg','-y','-ss',str(clip.start),'-t',str(dur),'-i',str(source),'-i',str(watermark),'-filter_complex',fc,'-map','[v]','-map','0:a?','-c:v','libx264','-preset','veryfast','-crf','21','-threads','0','-c:a','aac','-b:a','160k','-movflags','+faststart',str(out)]
    else:
        cmd=['ffmpeg','-y','-ss',str(clip.start),'-t',str(dur),'-i',str(source),'-vf',f'crop=ih*9/16:ih,scale=1080:1920,{af}','-map','0:v','-map','0:a?','-c:v','libx264','-preset','veryfast','-crf','21','-threads','0','-c:a','aac','-b:a','160k','-movflags','+faststart',str(out)]
    run_cmd(cmd); ass.unlink(missing_ok=True)

def social_package(clips,instruction):
    if not client: return {}
    brief='\n'.join(f'{i+1}. {c.title} | {c.hook} | {c.category} | {c.score}' for i,c in enumerate(clips))
    try:
        r=client.responses.create(model=TEXT_MODEL,input=f'''Create concise social metadata for these clips. Instruction: {instruction or 'maximize clarity and shareability'}. Return ONLY JSON with keys youtube_shorts,tiktok,instagram. Each value has title,caption,hashtags(array). Do not invent facts.\n{brief}''')
        return json.loads(r.output_text.strip().replace('```json','').replace('```','').strip())
    except Exception: return {}

def render_one(args):
    job_id,video,transcript,clip,i,wm=args; out=video.parent/f'clip_{i}.mp4'; render_clip(video,transcript,clip,out,wm); return {'index':i,**clip.model_dump(),'download_url':f'/download/{job_id}/{i}'}

def pipeline(job_id,video,watermark,max_clips,instruction):
    try:
        JOBS[job_id]['status']='transcribing'; JOBS[job_id]['progress']=10; transcript=transcribe(video,video.parent)
        JOBS[job_id]['status']='scoring_highlights'; JOBS[job_id]['progress']=35; clips=find_highlights(transcript,max_clips,instruction)
        JOBS[job_id].update(status='rendering',progress=40,total_clips=len(clips),completed_clips=0,render_workers=RENDER_WORKERS)
        args=[(job_id,video,transcript,c,i,watermark) for i,c in enumerate(clips)]
        results=[None]*len(args)
        with ThreadPoolExecutor(max_workers=min(RENDER_WORKERS,len(args))) as pool:
            futures={pool.submit(render_one,a):i for i,a in enumerate(args)}
            for n,f in enumerate(futures):
                i=futures[f]; results[i]=f.result(); JOBS[job_id]['completed_clips']=n+1; JOBS[job_id]['progress']=40+int((n+1)/len(args)*50)
        JOBS[job_id]['status']='creating_social_package'; JOBS[job_id]['progress']=95; JOBS[job_id].update(status='done',progress=100,clips=results,social=social_package(clips,instruction))
    except Exception as e: JOBS[job_id].update(status='failed',error=str(e))

@app.get('/',response_class=HTMLResponse)
def root():
    ui=Path(__file__).resolve().parent.parent/'frontend'/'index.html'
    return HTMLResponse(ui.read_text(encoding='utf-8')) if ui.exists() else HTMLResponse('<h1>AutoClipper V3</h1><p>Frontend not found.</p>',status_code=500)
@app.head('/')
def root_head(): return HTMLResponse('')
@app.get('/health')
def health(): return {'status':'healthy','version':'3.1.0','openai_configured':bool(OPENAI_API_KEY),'text_model':TEXT_MODEL,'transcribe_model':TRANSCRIBE_MODEL,'render_workers':RENDER_WORKERS}
@app.post('/process')
async def process_video(background_tasks:BackgroundTasks,file:UploadFile=File(...),watermark:Optional[UploadFile]=File(None),max_clips:int=5,instruction:str=''):
    if not client: raise HTTPException(503,'OPENAI_API_KEY is not configured in Render')
    if not file.filename: raise HTTPException(400,'A video file is required')
    max_clips=max(1,min(max_clips,10)); job_id=str(uuid.uuid4()); job_dir=WORK_DIR/job_id; job_dir.mkdir(parents=True,exist_ok=True); video=job_dir/'source.mp4'
    with video.open('wb') as out: shutil.copyfileobj(file.file,out)
    if video.stat().st_size>MAX_UPLOAD_MB*1024*1024: shutil.rmtree(job_dir,ignore_errors=True); raise HTTPException(413,f'Video exceeds {MAX_UPLOAD_MB} MB')
    wm=None
    if watermark:
        wm=job_dir/'watermark.png'
        with wm.open('wb') as out: shutil.copyfileobj(watermark.file,out)
    JOBS[job_id]={'status':'queued','progress':0,'clips':[],'instruction':instruction}; background_tasks.add_task(pipeline,job_id,video,wm,max_clips,instruction); return {'job_id':job_id,'status':'queued','version':'3.1.0'}
@app.get('/status/{job_id}')
def status(job_id:str):
    if job_id not in JOBS: raise HTTPException(404,'job not found')
    return JOBS[job_id]
@app.get('/download/{job_id}/{clip_index}')
def download(job_id:str,clip_index:int):
    path=WORK_DIR/job_id/f'clip_{clip_index}.mp4'
    if not path.exists(): raise HTTPException(404,'clip not found')
    return FileResponse(path,media_type='video/mp4',filename=path.name)
