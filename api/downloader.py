from http.server import BaseHTTPRequestHandler
import json
import os
import urllib.parse
import urllib.request
import urllib.error

COBALT_API_URL = os.environ.get("COBALT_API_URL", "https://api.cobalt.tools/")
COBALT_API_KEY = os.environ.get("COBALT_API_KEY", "")

ALLOWED_PROXY_SUFFIXES = (
    "cdninstagram.com",
    "fbcdn.net",
    urllib.parse.urlparse(COBALT_API_URL).hostname or "",
)

def host_allowed(url):
    try:
        p = urllib.parse.urlparse(url)
        host = (p.hostname or "").lower()
        return p.scheme in ("http", "https") and any(
            host == s or host.endswith("." + s) for s in ALLOWED_PROXY_SUFFIXES if s
        )
    except Exception:
        return False


class handler(BaseHTTPRequestHandler):
    def _json(self, code, payload):
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(json.dumps(payload).encode('utf-8'))

    def do_GET(self):
        if '?url=' in self.path:
            try:
                q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                target_url = q.get('url', [None])[0]
                filename = q.get('filename', ['instagram-media'])[0].replace('"', '')

                if not target_url or not host_allowed(target_url):
                    return self._json(400, {'error': 'Invalid or disallowed URL'})

                req = urllib.request.Request(
                    target_url,
                    headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
                )
                with urllib.request.urlopen(req, timeout=25) as remote:
                    self.send_response(200)
                    self.send_header('Content-Type', remote.headers.get('Content-Type', 'application/octet-stream'))
                    self.send_header('Content-Disposition', f'attachment; filename="{filename}"')
                    self.send_header('Access-Control-Allow-Origin', '*')
                    self.end_headers()
                    while True:
                        chunk = remote.read(8192)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                return
            except Exception as e:
                return self._json(500, {'error': str(e)})

        self._json(200, {'status': 'API is live and running'})

    def do_POST(self):
        try:
            length = int(self.headers.get('Content-Length', 0))
            data = json.loads(self.rfile.read(length).decode('utf-8'))
            raw_url = data.get('url', '')

            if not raw_url or 'instagram.com' not in raw_url:
                return self._json(400, {'error': 'Invalid Instagram URL'})

            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                'Accept': 'application/json',
                'Content-Type': 'application/json',
            }
            if COBALT_API_KEY:
                headers['Authorization'] = f'Api-Key {COBALT_API_KEY}'

            req = urllib.request.Request(
                COBALT_API_URL,
                data=json.dumps({"url": raw_url, "videoQuality": "max"}).encode('utf-8'),
                headers=headers,
                method='POST'
            )

            try:
                with urllib.request.urlopen(req, timeout=25) as resp:
                    res = json.loads(resp.read().decode('utf-8', errors='ignore'))
            except urllib.error.HTTPError as he:
                body = he.read().decode('utf-8', errors='ignore')
                return self._json(502, {'error': f'Extractor returned {he.code}', 'details': body[:500]})

            status = res.get('status')
            media_urls, is_image = [], True

            if status == 'picker':
                items = res.get('picker', [])
                media_urls = [i['url'] for i in items if i.get('url')]
                is_image = all(i.get('type', 'photo') == 'photo' for i in items)
            elif status in ('tunnel', 'redirect', 'stream'):
                u = res.get('url')
                if u:
                    media_urls.append(u)
                    name = (res.get('filename') or u).lower()
                    is_image = not any(ext in name for ext in ('.mp4', '.webm', '.mov'))
            elif status == 'error':
                code = (res.get('error') or {}).get('code', 'unknown')
                return self._json(502, {'error': f'Extractor error: {code}'})

            if media_urls:
                self._json(200, {'download_urls': media_urls, 'is_image': is_image})
            else:
                self._json(500, {'error': 'Could not extract media. Please check if the post is public.'})

        except Exception as e:
            self._json(500, {'error': str(e)})

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()
