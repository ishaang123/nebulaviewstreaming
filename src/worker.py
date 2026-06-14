from workers import WorkerEntrypoint
from fastapi import FastAPI, Request
from fastapi.responses import Response, StreamingResponse, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
import asgi
import urllib.parse
import httpx
import re

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# A simplified version of your HTML player layout
PLAYER_HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>NebulaView Player</title>
    <link href="https://vjs.zencdn.net/8.10.0/video-js.css" rel="stylesheet" />
    <script src="https://vjs.zencdn.net/8.10.0/video.min.js"></script>
</head>
<body style="background:#111; color:#fff; text-align:center; font-family:sans-serif;">
    <h2>NebulaView Single-Server Player</h2>
    <div style="max-width: 800px; margin: 20px auto;">
        <video id="my-video" class="video-js vjs-default-skin vjs-16-9" controls preload="auto">
            <source src="/manifest?url={stream_url}" type="application/x-mpegURL">
        </video>
    </div>
    <script>
        var player = videojs('my-video');
    </script>
</body>
</html>
"""

# ROUTE 1: The Main Web Page
@app.get("/watch/{video_id}")
async def watch_video(video_id: str, request: Request):
    # Native lightweight extraction instead of yt_dlp
    # Dailymotion provides a direct internal metadata URL for every video ID
    dm_metadata_url = f"https://www.dailymotion.com/player/metadata/video/{video_id}"
    
    headers = {"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X)"}
    
    async with httpx.AsyncClient() as client:
        resp = await client.get(dm_metadata_url, headers=headers)
        
    if resp.status_code != 200:
        return HTMLResponse("content=","<h1>Video not found or blocked</h1>", status_code=404)
    
    # Extract the master .m3u8 link from Dailymotion's JSON response
    data = resp.json()
    qualities = data.get("qualities", {})
    
    # Find the best available live streaming playlist link
    stream_url = None
    for q in ["auto", "1080", "720", "480", "360"]:
        if q in qualities:
            stream_url = qualities[q][0].get("url")
            break
            
    if not stream_url:
        return HTMLResponse("<h1>Could not extract stream stream metadata</h1>", status_code=500)

    # Encode the URL so it passes cleanly into our proxy route
    current_host = str(request.base_url).rstrip('/')
    encoded_stream = urllib.parse.quote_plus(stream_url)
    
    # Render your page template dynamically
    final_html = PLAYER_HTML.format(stream_url=encoded_stream)
    return HTMLResponse(content=final_html)

# ROUTE 2: The Playlist Manifest Rewriter
@app.get("/manifest")
async def proxy_m3u8(url: str, request: Request):
    raw_m3u8_url = urllib.parse.unquote(url)
    headers = {'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X)'}
    
    async with httpx.AsyncClient() as client:
        resp = await client.get(raw_m3u8_url, headers=headers, timeout=5.0)

    base_url = raw_m3u8_url.rsplit('/', 1)[0] + '/'
    rewritten_lines = []
    current_host = str(request.base_url).rstrip('/')

    for line in resp.text.splitlines():
        line_stripped = line.strip()
        if not line_stripped:
            continue

        if 'URI=' in line_stripped:
            def replace_uri(match):
                rel_path = match.group(1).strip('"\'')
                abs_url = urllib.parse.urljoin(base_url, rel_path)
                proxy_route = "/manifest" if (".m3u8" in rel_path or "manifest" in rel_path) else "/segment"
                return f'URI="{current_host}{proxy_route}?url={urllib.parse.quote_plus(abs_url)}"'
            line_stripped = re.sub(r'URI=(["\'].*?["\'])', replace_uri, line_stripped)
            rewritten_lines.append(line_stripped)

        elif not line_stripped.startswith('#'):
            full_url = line_stripped if line_stripped.startswith(('http://', 'https://')) else urllib.parse.urljoin(base_url, line_stripped)
            encoded_url = urllib.parse.quote_plus(full_url)
            if '.m3u8' in line_stripped or 'manifest' in line_stripped:
                rewritten_lines.append(f"{current_host}/manifest?url={encoded_url}")
            else:
                rewritten_lines.append(f"{current_host}/segment?url={encoded_url}")
        else:
            rewritten_lines.append(line_stripped)

    return Response(content="\n".join(rewritten_lines), media_type="application/vnd.apple.mpegurl")

# ROUTE 3: The Video Segment Data Pipeline
@app.get("/segment")
async def proxy_ts_segment(url: str):
    raw_ts_url = urllib.parse.unquote(url)
    headers = {
        'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X)',
        'Accept': '*/*',
        'Connection': 'keep-alive'
    }

    client = httpx.AsyncClient()
    content_type = 'video/mp4' if ('.mp4' in raw_ts_url or '/fmp4/' in raw_ts_url) else 'video/MP2T'

    async def stream_ts_data():
        async with client.stream("GET", raw_ts_url, headers=headers, timeout=7.0) as r:
            async for block in r.aiter_bytes(chunk_size=16384):
                yield block

    return StreamingResponse(stream_ts_data(), media_type=content_type)

# Mount application to Cloudflare Workers Core
class Default(WorkerEntrypoint):
    async def fetch(self, request):
        return await asgi.fetch(app, request, self.env)
