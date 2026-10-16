"""Video downloader for public, authorized content. No DRM / paywall bypass."""
import asyncio
import ipaddress
import os
import re
import shutil
import socket
import tempfile
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse, quote

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from starlette.background import BackgroundTask
import yt_dlp
from yt_dlp.utils import DownloadError

APP_HOST = os.getenv("PUBLIC_API_URL", "").rstrip("/")
ORIGINS = [x.strip() for x in os.getenv("ALLOWED_ORIGINS", "http://localhost:8000").split(",") if x.strip()]
app = FastAPI(title="VYDEO API", docs_url=None, redoc_url=None)
app.add_middleware(CORSMiddleware, allow_origins=ORIGINS, allow_methods=["GET","POST"], allow_headers=["Content-Type"], max_age=600)
# Only publicly hosted URLs for supported media platforms. Prevent generic web proxy SSRF.
SUPPORTED = ("youtube.com", "youtu.be", "tiktok.com", "instagram.com", "x.com", "twitter.com", "vimeo.com", "dailymotion.com", "twitch.tv", "reddit.com", "facebook.com", "fb.watch", "streamable.com", "soundcloud.com")
JOBS = {}
LOCK = asyncio.Semaphore(2)
TTL = 600
class Input(BaseModel):
    url: str

def allowed_url(raw):
    if len(raw)>2048: return False
    try:
        p=urlparse(raw)
        host=(p.hostname or "").lower().rstrip(".")
        if p.scheme!='https' or p.username or p.password or p.port not in (None,443):return False
        if not any(host==d or host.endswith('.'+d) for d in SUPPORTED):return False
        for item in socket.getaddrinfo(host,443,type=socket.SOCK_STREAM):
            if not ipaddress.ip_address(item[4][0]).is_global:return False
        return True
    except (ValueError, socket.gaierror, OSError): return False

def public_error(exc):
    """Return safe diagnostic strings; never leak cookies, raw URLs or server details."""
    msg = str(exc).lower()
    if 'sign in to confirm' in msg or 'not a bot' in msg:
        return 'YouTube側のBot検知により、Renderからの取得が拒否されました。APIの接続は正常です。yt-dlp更新でも解消しない場合があります。'
    if 'private video' in msg or 'video is private' in msg:
        return 'この動画は非公開です。'
    if 'login required' in msg or 'sign in' in msg or 'login to' in msg:
        return 'この動画にはログインが必要です。公開APIでは認証付き動画を取得できません。'
    if 'unsupported url' in msg or 'no suitable extractor' in msg:
        return 'この動画URLは現在サポートされていません。'
    if 'drm' in msg:
        return 'この動画はDRMで保護されているため取得できません。'
    if 'requested format is not available' in msg:
        return '指定した画質や形式は利用できません。別の画質をお試しください。'
    if '429' in msg or 'too many requests' in msg:
        return '配信元のアクセス回数制限に達しました。しばらくしてからお試しください。'
    if '403' in msg or 'forbidden' in msg:
        return '配信元からアクセスを拒否されました（403）。'
    return '動画を取得できませんでした。配信元の制限、動画の公開状態、対応状況などを確認してください。'

def clean():
    now=time.time()
    for token,job in list(JOBS.items()):
        if now-job['created']>TTL:
            JOBS.pop(token,None)

def inspect(url):
    options={'quiet':True,'no_warnings':True,'skip_download':True,'noplaylist':True,'socket_timeout':15,'retries':1,'extractor_retries':1, 'ignoreconfig':True}
    with yt_dlp.YoutubeDL(options) as ydl:
        info=ydl.extract_info(url,download=False)
        if info and info.get('_type')=='playlist':raise ValueError('プレイリストには対応していません')
        return {'title':(info.get('title') or 'Video')[:200], 'thumbnail': info.get('thumbnail'), 'duration': info.get('duration'), 'extractor': info.get('extractor_key')}

def save_video(url, mode, folder):
    out=str(folder/'%(title).90s-%(id)s.%(ext)s')
    opts={'outtmpl':out,'quiet':True,'no_warnings':True,'noplaylist':True,'socket_timeout':20,'retries':2,'fragment_retries':2, 'ignoreconfig':True, 'restrictfilenames':True, 'overwrites':False}
    if mode=='mp3':
        opts.update(format='bestaudio/best',postprocessors=[{'key':'FFmpegExtractAudio','preferredcodec':'mp3','preferredquality':'192'}])
    else:
        quality=int(mode)
        opts.update(format=f'bestvideo[height<={quality}]+bestaudio/best[height<={quality}]/best',merge_output_format='mp4',postprocessors=[{'key':'FFmpegVideoRemuxer','preferedformat':'mp4'}])
    with yt_dlp.YoutubeDL(opts) as ydl: ydl.download([url])
    items=[f for f in folder.iterdir() if f.is_file() and f.suffix.lower() in ('.mp4','.mp3')]
    if not items: raise RuntimeError('動画ファイルを生成できませんでした')
    return max(items,key=lambda x:x.stat().st_size)

@app.get('/health')
def health():return {'ok':True,'yt_dlp_version':yt_dlp.version.__version__}

@app.post('/api/info')
async def info(req: Input, request:Request):
    clean()
    if not allowed_url(req.url):raise HTTPException(400,'このURLは未対応、または安全に検証できませんでした')
    async with LOCK:
        try: data=await asyncio.wait_for(asyncio.to_thread(inspect,req.url),timeout=50)
        except Exception as exc: raise HTTPException(422,public_error(exc)) from exc
    token=uuid.uuid4().hex
    JOBS[token]={'url':req.url,'created':time.time()}
    origin=APP_HOST or str(request.base_url).rstrip('/')
    values=[('最高画質 MP4','2160'),('1080p MP4','1080'),('720p MP4','720'),('480p MP4','480'),('MP3 音声','mp3')]
    formats=[{'label':label,'url':f'{origin}/api/download/{token}/{mode}'} for label,mode in values]
    return {**data,'formats':formats}

@app.get('/api/download/{token}/{mode}')
async def download(token:str,mode:str):
    clean()
    if mode not in ('2160','1080','720','480','mp3') or token not in JOBS:raise HTTPException(404,'リンクの期限切れまたは形式が無効です。再解析してください')
    url=JOBS[token]['url']
    if not allowed_url(url):raise HTTPException(400,'URLの検証に失敗しました')
    folder=Path(tempfile.mkdtemp(prefix='vydeo_'))
    try:
        async with LOCK:
            file=await asyncio.wait_for(asyncio.to_thread(save_video,url,mode,folder),timeout=240)
        filename='vydeo-'+token[:10]+file.suffix
        return FileResponse(file,media_type='audio/mpeg' if mode=='mp3' else 'video/mp4',filename=filename,background=BackgroundTask(shutil.rmtree,folder,True))
    except Exception as exc:
        shutil.rmtree(folder,ignore_errors=True)
        raise HTTPException(422,public_error(exc)) from exc
