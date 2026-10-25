"""TikGlass API. Supports only publicly accessible TikTok URLs."""
import io
import ipaddress
import os
import re
import socket
import tempfile
import zipfile
from pathlib import Path
from urllib.parse import urlparse

import httpx
import yt_dlp
from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field

app = FastAPI(title="TikGlass API", docs_url=None, redoc_url=None)
MAX_FILE_BYTES = 150 * 1024 * 1024
MAX_IMAGES = 35

class InspectRequest(BaseModel):
    url: str = Field(max_length=2048)

class DownloadRequest(InspectRequest):
    type: str
    quality: str = "best"

def allowed_url(url: str) -> str:
    p = urlparse(url.strip())
    host = (p.hostname or '').lower().rstrip('.')
    if p.scheme != 'https' or p.username or p.password or p.port not in (None, 443):
        raise HTTPException(400, "Utiliza un enlace HTTPS válido de TikTok.")
    if host not in ('tiktok.com', 'www.tiktok.com', 'm.tiktok.com', 'vm.tiktok.com', 'vt.tiktok.com'):
        raise HTTPException(400, "Solo se aceptan enlaces de TikTok.")
    return url.strip()

def extract(url):
    opts = {'quiet': True, 'no_warnings': True, 'noplaylist': True,
            'skip_download': True, 'socket_timeout': 14, 'retries': 1,
            'extractor_retries': 1, 'ignoreerrors': False}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
        if not info:
            raise ValueError('La publicación no está disponible.')
        return info
    except (yt_dlp.utils.DownloadError, ValueError) as e:
        raise HTTPException(422, "TikTok no permitió acceder a este contenido público. Puede estar restringido, eliminado o haber cambiado su formato.") from e

def photo_urls(info):
    # TikTok photo carousel support varies with extractor version and post.
    photos = []
    for entry in info.get('entries') or []:
        if not isinstance(entry, dict):
            continue
        url = entry.get('url')
        if isinstance(url, str) and (entry.get('ext') in ('jpg','jpeg','png','webp') or re.search(r'\.(jpg|jpeg|png|webp)(\?|$)', url, re.I)):
            photos.append(url)
    for entry in info.get('images') or []:
        if isinstance(entry, str):
            photos.append(entry)
        elif isinstance(entry, dict):
            url = entry.get('url')
            if isinstance(url, str):
                photos.append(url)
    return list(dict.fromkeys(photos))[:MAX_IMAGES]

def formats(info):
    options = []
    heights = set()
    for item in info.get('formats') or []:
        if item.get('vcodec') == 'none':
            continue
        height = item.get('height')
        if isinstance(height, int) and height > 0:
            heights.add(height)
    for h in sorted(heights, reverse=True):
        options.append({'id': str(h), 'label': f'{h}p · resolución disponible'})
    if not options and (info.get('vcodec') not in (None, 'none') or info.get('formats')):
        options = [{'id':'best','label':'Mejor calidad disponible'}]
    return options

def file_response(data, name, mime):
    if len(data) > MAX_FILE_BYTES:
        raise HTTPException(413, 'El archivo supera el límite de 150 MB.')
    return Response(content=data, media_type=mime,
                    headers={'Content-Disposition':f'attachment; filename="{name}"',
                             'Cache-Control':'no-store', 'X-Content-Type-Options':'nosniff'})

def safe_media_url(url):
    """Allow CDN image URLs only, and prevent requests to private network IPs."""
    p = urlparse(url)
    h = (p.hostname or '').lower().rstrip('.')
    if p.scheme != 'https' or p.username or p.password or p.port not in (None,443):
        return False
    domains = ('tiktokcdn.com', 'tiktokcdn-us.com', 'tiktok.com', 'byteimg.com', 'ibytedtos.com', 'muscdn.com', 'tiktokv.com')
    if not any(h == d or h.endswith('.'+d) for d in domains):
        return False
    try:
        addresses = socket.getaddrinfo(h, 443)
        return bool(addresses) and all(ipaddress.ip_address(r[4][0]).is_global for r in addresses)
    except (socket.gaierror, ValueError):
        return False

@app.get('/api/health')
def health():
    return {'ok': True}

@app.post('/api/inspect')
def inspect(body: InspectRequest):
    url = allowed_url(body.url)
    info = extract(url)
    imgs = photo_urls(info)
    opts = formats(info)
    thumb = info.get('thumbnail') or ''
    return {'title': (info.get('title') or info.get('description') or 'TikTok')[:180],
            'author': info.get('uploader_id') or info.get('uploader') or '',
            'thumbnail': thumb if isinstance(thumb, str) and thumb.startswith('https://') else '',
            'qualities': opts, 'has_audio': bool(opts or info.get('acodec') not in (None,'none')),
            'photo_count':len(imgs)}

@app.post('/api/download')
def download(body: DownloadRequest):
    url = allowed_url(body.url)
    kind = body.type
    if kind not in ('mp4', 'mp3', 'images'):
        raise HTTPException(400, 'Formato no válido.')
    if kind == 'images':
        info = extract(url)
        images = photo_urls(info)
        if not images:
            thumbnail = info.get('thumbnail')
            if not thumbnail or not safe_media_url(thumbnail):
                raise HTTPException(422, 'No se encontraron imágenes descargables.')
            images = [thumbnail]
        if not all(safe_media_url(u) for u in images):
            raise HTTPException(422, 'TikTok proporcionó imágenes desde un dominio no admitido.')
        memory = io.BytesIO()
        with httpx.Client(timeout=20, follow_redirects=False) as client:
            if len(images) == 1:
                resp = client.get(images[0]); resp.raise_for_status()
                if len(resp.content) > MAX_FILE_BYTES:
                    raise HTTPException(413, 'Imagen demasiado grande.')
                mime=resp.headers.get('content-type','image/jpeg').split(';')[0]
                extension='png' if 'png' in mime else 'webp' if 'webp' in mime else 'jpg'
                return file_response(resp.content, f'tikglass-imagen.{extension}',mime)
            with zipfile.ZipFile(memory,'w',zipfile.ZIP_DEFLATED) as z:
                for i, image_url in enumerate(images,1):
                    r=client.get(image_url); r.raise_for_status()
                    if len(r.content)>12*1024*1024:
                        raise HTTPException(413,'Una imagen es demasiado grande.')
                    mime=r.headers.get('content-type','image/jpeg')
                    ext='png' if 'png' in mime else 'webp' if 'webp' in mime else 'jpg'
                    z.writestr(f'foto-{i:02d}.{ext}', r.content)
                    if memory.tell()>MAX_FILE_BYTES:
                        raise HTTPException(413,'El ZIP es demasiado grande.')
        return file_response(memory.getvalue(),'tikglass-fotos.zip','application/zip')

    if kind == 'mp3' and body.quality not in ('128','192','320'):
        raise HTTPException(400,'Calidad MP3 no válida.')
    if kind == 'mp4' and body.quality != 'best' and not (body.quality.isdigit() and 1 <= int(body.quality) <= 4320):
        raise HTTPException(400,'Resolución no válida.')
    with tempfile.TemporaryDirectory() as tmp:
        opts = {'quiet': True, 'no_warnings': True, 'noplaylist': True,
                'outtmpl': os.path.join(tmp,'video.%(ext)s'),
                'socket_timeout': 20, 'retries': 1,
                'max_filesize': MAX_FILE_BYTES, 'restrictfilenames': True}
        if kind == 'mp3':
            opts['format'] = 'bestaudio/best'
            opts['postprocessors'] = [{'key':'FFmpegExtractAudio','preferredcodec':'mp3',
                                       'preferredquality':body.quality}]
        else:
            h=body.quality
            opts['format'] = 'bestvideo[height<='+h+']+bestaudio/best[height<='+h+']/best' if h!='best' else 'bestvideo+bestaudio/best'
            opts['merge_output_format']='mp4'
            opts['postprocessors']=[{'key':'FFmpegVideoConvertor','preferedformat':'mp4'}]
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([url])
        except yt_dlp.utils.DownloadError as e:
            raise HTTPException(422,'No se pudo descargar o convertir el contenido. Prueba otra calidad o enlace.') from e
        extension=kind
        matches=sorted(Path(tmp).glob('*.'+extension),key=lambda p:p.stat().st_size,reverse=True)
        if not matches:
            raise HTTPException(422,'TikTok no proporcionó este formato para la publicación.')
        p=matches[0]
        if p.stat().st_size>MAX_FILE_BYTES:
            raise HTTPException(413,'El archivo supera el límite de 150 MB.')
        mime='audio/mpeg' if kind=='mp3' else 'video/mp4'
        return file_response(p.read_bytes(),f'tikglass-video.{extension}',mime)
