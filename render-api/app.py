import time, uuid, shutil, subprocess, logging, json, re
from pathlib import Path
from urllib.parse import urlsplit
from concurrent.futures import ThreadPoolExecutor
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
import yt_dlp

ROOT = Path(__file__).parent
DATA = ROOT / 'downloads'
DATA.mkdir(exist_ok=True)
app = FastAPI()
app.mount('/static', StaticFiles(directory=ROOT/'static'), name='static')
pool = ThreadPoolExecutor(max_workers=1)
jobs = {}
LIMIT = 1024*1024*1024
class Request(BaseModel):
    url: str = Field(min_length=1, max_length=2048)
    quality: int = 1080

def validate(url):
    try:
        p = urlsplit(url)
        h = (p.hostname or '').lower()
        port = p.port
    except ValueError:
        raise HTTPException(400, '正しい動画URLを入力してください。')
    if p.scheme != 'https' or p.username or p.password or port not in (None,443):
        raise HTTPException(400, 'HTTPSの動画URLを入力してください。')
    if h not in {'youtube.com','www.youtube.com','m.youtube.com','youtu.be','www.tiktok.com','tiktok.com','vm.tiktok.com','vt.tiktok.com'}:
        raise HTTPException(400, 'YouTube・TikTokの動画URLに対応しています。')
    if h.endswith('youtube.com') and not (p.path == '/watch' or p.path.startswith(('/shorts/','/live/'))):
        raise HTTPException(400, 'チャンネルではなく動画のURLを入力してください。')
    return url

def convert(source, output):
    probe = subprocess.run(
        ['ffprobe', '-v', 'error', '-show_streams', '-of', 'json', str(source)],
        capture_output=True, text=True, check=True, timeout=30,
    )
    streams = json.loads(probe.stdout)['streams']
    video = next((s for s in streams if s['codec_type']=='video'), None)
    audio = next((s for s in streams if s['codec_type']=='audio'), None)
    if not video:
        raise ValueError('動画トラックがありません。')
    copy_video = video.get('codec_name')=='h264' and video.get('pix_fmt')=='yuv420p'
    video_options = ['-c:v','copy'] if copy_video else ['-c:v','libx264','-preset','veryfast','-crf','20','-pix_fmt','yuv420p','-threads','2']
    audio_options = ['-c:a','copy'] if not audio or audio.get('codec_name')=='aac' else ['-c:a','aac','-b:a','192k']
    subprocess.run(
        ['ffmpeg','-hide_banner','-loglevel','error','-y','-i',str(source),
         '-map','0:v:0','-map','0:a:0?', *video_options, *audio_options,
         '-movflags','+faststart',str(output)],
        check=True,timeout=900,capture_output=True,
    )

def run(id, url, quality):
    j = jobs[id]
    folder = DATA/id
    folder.mkdir()
    def progress(d):
        if d['status']=='downloading':
            total=d.get('total_bytes') or d.get('total_bytes_estimate') or 0
            done=d.get('downloaded_bytes',0)
            if done>LIMIT: raise ValueError('ファイルの上限1GBを超えました。')
            j.update(state='downloading', percent=min(94,round(done/total*94,1)) if total else 0)
        elif d['status']=='finished': j.update(state='converting', percent=95)
    try:
        j.update(state='analyzing')
        if not shutil.which('ffmpeg') or not shutil.which('ffprobe'): raise ValueError('サーバーにFFmpegがありません。READMEのセットアップを実行してください。')
        opts={'quiet':True,'noplaylist':True,'socket_timeout':25,'retries':2,'fragment_retries':2,
              'outtmpl':str(folder/'source.%(ext)s'),'max_filesize':LIMIT,
              'format':f'bv*[height<={quality}]+ba/b[height<={quality}]',
              'merge_output_format':'mp4','progress_hooks':[progress],
              'js_runtimes':{'deno':{},'node':{}},'restrictfilenames':True}
        with yt_dlp.YoutubeDL(opts) as ydl:
            info=ydl.extract_info(url, download=False)
            if not info or not isinstance(info,dict): raise ValueError('動画情報を取得できませんでした。')
            if info.get('_type') in ('playlist','multi_video') or info.get('entries'): raise ValueError('動画1本のURLを指定してください。')
            if info.get('is_live'): raise ValueError('配信中のライブには対応していません。')
            if (info.get('duration') or 0)>1800: raise ValueError('30分以内の動画を指定してください。')
            j.update(title=info.get('title','video'),duration=info.get('duration'),state='downloading')
            ydl.process_ie_result(info, download=True)
        sources=[p for p in folder.iterdir() if p.suffix in {'.mp4','.webm','.mkv','.mov'}]
        if len(sources)!=1: raise ValueError('動画ファイルの結合に失敗しました。')
        j.update(state='converting',percent=96)
        output=folder/'video.mp4'
        convert(sources[0], output)
        sources[0].unlink(missing_ok=True)
        if output.stat().st_size>LIMIT: raise ValueError('出力ファイルの上限1GBを超えました。')
        j.update(state='ready',percent=100,size=output.stat().st_size)
    except Exception as e:
        logging.exception('Download failed')
        msg=re.sub(r'\x1b\[[0-9;]*m','',str(e))
        if 'Sign in' in msg or 'bot' in msg: msg='配信元がサーバーからの取得を制限しています。PCでのローカル起動を試してください。'
        elif 'Private' in msg or 'login' in msg: msg='非公開・ログインが必要な動画は取得できません。'
        elif 'Invalid data found' in msg or isinstance(e,subprocess.CalledProcessError): msg='正常な動画データを取得・変換できませんでした。配信元の通信制限やサーバーログを確認してください。'
        j.update(state='error',error=msg[:600])
        shutil.rmtree(folder,ignore_errors=True)
    finally: j['finished']=time.time()

@app.get('/')
def index(): return FileResponse(ROOT/'static/index.html')
@app.head('/')
def head(): return {}
@app.get('/health')
def health(): return {'status':'ok','ffmpeg':bool(shutil.which('ffmpeg')),'runtime':bool(shutil.which('deno') or shutil.which('node'))}
@app.post('/api/jobs')
def create(r: Request):
    url=validate(r.url.strip())
    if r.quality not in (360,720,1080,2160): raise HTTPException(400,'画質を選択してください。')
    for key,j in list(jobs.items()):
        if j.get('finished',time.time()) < time.time()-3600:
            shutil.rmtree(DATA/key,ignore_errors=True);jobs.pop(key,None)
    if sum(j['state'] not in ('ready','error') for j in jobs.values())>=4:
        raise HTTPException(429,'現在処理中です。少し待って再試行してください。')
    id=uuid.uuid4().hex
    jobs[id]={'state':'queued','percent':0}
    pool.submit(run,id,url,r.quality)
    return {'id':id}
@app.get('/api/jobs/{id}')
def status(id: str):
    if id not in jobs: raise HTTPException(404,'有効期限が切れました。もう一度取得してください。')
    return jobs[id]
@app.get('/api/jobs/{id}/file')
def file(id: str):
    if id not in jobs or jobs[id]['state']!='ready': raise HTTPException(404,'保存できるファイルがありません。')
    title=''.join(c for c in jobs[id]['title'] if c not in '/\\:*?"<>|')[:100] or 'video'
    return FileResponse(DATA/id/'video.mp4',media_type='video/mp4',filename=title+'.mp4',headers={'Cache-Control':'no-store'})
