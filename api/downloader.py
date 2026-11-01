from http.server import BaseHTTPRequestHandler
import json
import re
import html as htmllib
import urllib.parse
import urllib.request
import yt_dlp

UA = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/124.0 Safari/537.36'
)

# The proxy (GET ?url=) only fetches from Instagram's CDN hosts
ALLOWED_PROXY_SUFFIXES = ('cdninstagram.com', 'fbcdn.net')


def host_allowed(url):
    try:
        p = urllib.parse.urlparse(url)
        host = (p.hostname or '').lower()
        return p.scheme in ('http', 'https') and any(
            host == s or host.endswith('.' + s) for s in ALLOWED_PROXY_SUFFIXES
        )
    except Exception:
        return False


def fetch_text(url):
    req = urllib.request.Request(
        url, headers={'User-Agent': UA, 'Accept-Language': 'en-US,en;q=0.9'}
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read().decode('utf-8', errors='ignore')


def extract_images_fallback(code):
    """Best-effort image extraction for public photo / carousel posts.
    yt-dlp only supports posts that contain video, so photo posts use this."""
    images = []

    def add(u):
        u = htmllib.unescape(u)
        if u.startswith('http') and u not in images:
            images.append(u)

    # 1) Embed page (often has all carousel images)
    try:
        page = fetch_text(f'https://www.instagram.com/p/{code}/embed/captioned/')
        for u in re.findall(r'"display_url"\s*:\s*"([^"]+)"', page):
            try:
                add(json.loads('"' + u + '"'))
            except Exception:
                pass
        for u in re.findall(r'class="EmbeddedMediaImage"[^>]*src="([^"]+)"', page):
            add(u)
    except Exception:
        pass

    # 2) Main page og:image (single image only)
    if not images:
        try:
            page = fetch_text(f'https://www.instagram.com/p/{code}/')
            m = re.search(r'<meta property="og:image" content="([^"]+)"', page)
            if m:
                add(m.group(1))
        except Exception:
            pass

    return images


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
                parsed_url = urllib.parse.urlparse(self.path)
                query_params = urllib.parse.parse_qs(parsed_url.query)
                target_url = query_params.get('url', [None])[0]
                filename = query_params.get('filename', ['instagram-media'])[0].replace('"', '')

                if not target_url or not host_allowed(target_url):
                    return self._json(400, {'error': 'Invalid or disallowed URL'})

                req = urllib.request.Request(target_url, headers={'User-Agent': UA})
                with urllib.request.urlopen(req, timeout=25) as remote_file:
                    content_type = remote_file.headers.get('Content-Type', 'application/octet-stream')
                    self.send_response(200)
                    self.send_header('Content-Type', content_type)
                    self.send_header('Content-Disposition', f'attachment; filename="{filename}"')
                    self.send_header('Access-Control-Allow-Origin', '*')
                    self.end_headers()

                    while True:
                        chunk = remote_file.read(8192)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                return
            except Exception as e:
                return self._json(500, {'error': str(e)})

        self._json(200, {'status': 'API is live and running'})

    def do_POST(self):
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length).decode('utf-8')
            data = json.loads(body)
            raw_url = data.get('url', '')

            if not raw_url or 'instagram.com' not in raw_url:
                return self._json(400, {'error': 'Invalid Instagram URL'})

            clean_url = raw_url.split('?')[0]
            match_post = re.search(
                r'instagram\.com/(?:[\w\.\-]+/)?(p|reel|reels|tv)/([\w\-]+)', clean_url
            )
            post_code = None
            if match_post:
                media_type = match_post.group(1)
                post_code = match_post.group(2)
                if media_type in ('reel', 'reels'):
                    clean_url = f'https://www.instagram.com/reel/{post_code}/'
                else:
                    clean_url = f'https://www.instagram.com/p/{post_code}/'

            media_urls = []
            is_image = True
            last_error = None

            ydl_opts = {
                'quiet': True,
                'no_warnings': True,
                'skip_download': True,
                'extractor_args': {'instagram': {'max_comments': [0]}},
            }

            # Step 1: yt-dlp (works for reels / video posts)
            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(clean_url, download=False)

                if info:
                    if 'entries' in info:
                        for entry in info['entries']:
                            if not entry:
                                continue
                            node_url = entry.get('url') or entry.get('thumbnail')
                            if node_url and node_url not in media_urls:
                                media_urls.append(node_url)
                            if entry.get('vcodec') not in (None, 'none'):
                                is_image = False
                    else:
                        has_video = (
                            info.get('vcodec') not in (None, 'none')
                            or info.get('ext') in ('mp4', 'webm')
                        )
                        vid_url = info.get('url')
                        if has_video and vid_url:
                            media_urls.append(vid_url)
                            is_image = False
                        elif info.get('thumbnail'):
                            media_urls.append(info['thumbnail'])
            except Exception as ydl_ex:
                last_error = str(ydl_ex)

            # Step 2: fallback for photo-only posts (yt-dlp can't handle them)
            if not media_urls and post_code:
                media_urls = extract_images_fallback(post_code)
                is_image = True

            if media_urls:
                cleaned_urls = list(dict.fromkeys(media_urls))  # dedupe, keep order
                return self._json(200, {'download_urls': cleaned_urls, 'is_image': is_image})

            self._json(500, {
                'error': 'Could not extract media. Please check if the post is public.',
                'details': (last_error or '')[:300],
            })

        except Exception as e:
            self._json(500, {'error': str(e)})

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()
