from js import Response, Headers
import urllib.parse
import re

# Standard HTML template using native video.js
PLAYER_HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>NebulaView Player</title>
    <link href="https://vjs.zencdn.net/8.10.0/video-js.css" rel="stylesheet" />
    <script src="https://vjs.zencdn.net/8.10.0/video.min.js"></script>
</head>
<body style="background:#111; color:#fff; text-align:center; font-family:sans-serif; margin-top:50px;">
    <h2>NebulaView Serverless Player Engine</h2>
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

async def on_fetch(request, env, ctx):
    # Native parsing of the request URL string
    url_obj = urllib.parse.urlparse(request.url)
    path = url_obj.path
    query_params = urllib.parse.parse_qs(url_obj.query)
    current_host = f"{url_obj.scheme}://{url_obj.netloc}"

    # Route 1: Watch Page Engine (/watch/video_id)
    if path.startswith("/watch/"):
        video_id = path.split("/")[-1]
        dm_metadata_url = f"https://www.dailymotion.com/player/metadata/video/{video_id}"
        
        # Use native Javascript fetch engine inside Python worker
        from js import fetch as js_fetch
        try:
            resp = await js_fetch(dm_metadata_url)
            if resp.status != 200:
                return Response.new("<h1>Video source tracking failed or blocked</h1>", headers={"Content-Type": "text/html"})
            
            data = await resp.json()
            # Convert Javascript map object to standard Python dictionary
            qualities = data.to_py().get("qualities", {})
        except Exception:
            return Response.new("<h1>Metadata parsing failed</h1>", headers={"Content-Type": "text/html"})
        
        stream_url = None
        for q in ["auto", "1080", "720", "480", "360"]:
            if q in qualities:
                stream_url = qualities[q][0].get("url")
                break
                
        if not stream_url:
            return Response.new("<h1>Could not extract streaming track profiles</h1>", headers={"Content-Type": "text/html"})

        encoded_stream = urllib.parse.quote_plus(stream_url)
        return Response.new(PLAYER_HTML.format(stream_url=encoded_stream), headers={"Content-Type": "text/html"})

    # Route 2: Live Manifest Parser (/manifest?url=...)
    elif path == "/manifest":
        url_param = query_params.get("url")
        if not url_param:
            return Response.new("Missing source URL parameter", status=400)
        
        raw_m3u8_url = urllib.parse.unquote(url_param[0])
        from js import fetch as js_fetch
        
        resp = await js_fetch(raw_m3u8_url)
        manifest_text = await resp.text()

        base_url = raw_m3u8_url.rsplit('/', 1)[0] + '/'
        rewritten_lines = []

        for line in manifest_text.splitlines():
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

        res_headers = Headers.new({"Content-Type": "application/vnd.apple.mpegurl", "Access-Control-Allow-Origin": "*"})
        return Response.new("\n".join(rewritten_lines), headers=res_headers)

    # Route 3: The 302 Redirect Hack Engine (/segment?url=...)
    elif path == "/segment":
        url_param = query_params.get("url")
        if not url_param:
            return Response.new("Missing video segment parameter", status=400)
            
        raw_ts_url = urllib.parse.unquote(url_param[0])
        
        # Native 302 redirect object definition
        redirect_headers = Headers.new({"Location": raw_ts_url, "Access-Control-Allow-Origin": "*"})
        return Response.new("", status=302, headers=redirect_headers)

    # Fallback layout index
    return Response.new("NebulaView Proxy Engine Operational", status=200)
