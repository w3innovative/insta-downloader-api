from http.server import BaseHTTPRequestHandler
import json
import urllib.parse
import urllib.request
import re

class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        query_path = self.path
        if '?url=' in query_path:
            try:
                parsed_url = urllib.parse.urlparse(query_path)
                query_params = urllib.parse.parse_qs(parsed_url.query)
                target_url = query_params.get('url', [None])[0]
                filename = query_params.get('filename', ['instagram-media'])[0]

                if not target_url:
                    self.send_response(400)
                    self.end_headers()
                    return

                req = urllib.request.Request(
                    target_url,
                    headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
                )
                with urllib.request.urlopen(req) as remote_file:
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
                self.send_response(500)
                self.send_header('Content-type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({'error': str(e)}).encode('utf-8'))
                return

        self.send_response(200)
        self.send_header('Content-type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(json.dumps({'status': 'API is live and running'}).encode('utf-8'))

    def do_POST(self):
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length).decode('utf-8')
            data = json.loads(body)
            raw_url = data.get('url', '')

            if not raw_url or 'instagram.com' not in raw_url:
                self.send_response(400)
                self.send_header('Content-type', 'application/json')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                self.wfile.write(json.dumps({'error': 'Invalid Instagram URL'}).encode('utf-8'))
                return

            clean_url = raw_url.split('?')[0]
            match_post = re.search(r'instagram\.com/(?:[\w\.\-]+/)?(p|reel|reels|tv)/([\w\-]+)', clean_url)
            if match_post:
                media_type = match_post.group(1)
                code = match_post.group(2)
                # Use Instagram's public embed endpoint which never blocks public posts
                embed_url = f'https://www.instagram.com/p/{code}/embed/captioned/'
            else:
                embed_url = clean_url

            media_url = None
            is_image = False

            try:
                req = urllib.request.Request(
                    embed_url,
                    headers={
                        'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1',
                        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                        'Accept-Language': 'en-US,en;q=0.5'
                    }
                )
                with urllib.request.urlopen(req) as resp:
                    html_content = resp.read().decode('utf-8', errors='ignore')

                    # 1. Check for video stream in embed
                    video_matches = re.findall(r'src="([^"]+\.mp4[^"]*)"', html_content)
                    if not video_matches:
                        video_matches = re.findall(r'"video_url"\s*:\s*"([^"]+)"', html_content)

                    if video_matches and 'mp4' in video_matches[0]:
                        media_url = video_matches[0].encode().decode('unicode-escape').replace('\\/', '&amp;').replace('&amp;', '&')
                        is_image = False
                    else:
                        # 2. Extract high-res image source from public embed HTML
                        img_matches = re.findall(r'class="EmbeddedMediaImage"[^>]+src="([^"]+)"', html_content)
                        if not img_matches:
                            img_matches = re.findall(r'src="([^"]+fbcdn\.net[^"]+)"', html_content)

                        if img_matches:
                            media_url = img_matches[0].encode().decode('unicode-escape').replace('&amp;', '&')
                            is_image = True
                        else:
                            # 3. Fallback to Open Graph
                            og_match = re.search(r'<meta property="og:image" content="([^"]+)"', html_content)
                            if og_match:
                                media_url = og_match.group(1).replace('&amp;', '&')
                                is_image = True
            except Exception as ex:
                pass

            if media_url:
                # Strip downscaling to pull max resolution available
                media_url = re.sub(r'/s\d+x\d+/', '/', media_url)
                media_url = re.sub(r'/p\d+x\d+/', '/', media_url)
                media_url = re.sub(r'/c\d+\.\d+\.\d+\.\d+/', '/', media_url)

                if any(img_ext in media_url.lower() for img_ext in ['.jpg', '.jpeg', '.webp', '.png']):
                    is_image = True

                self.send_response(200)
                self.send_header('Content-type', 'application/json')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                self.wfile.write(json.dumps({'download_url': media_url, 'is_image': is_image}).encode('utf-8'))
            else:
                self.send_response(500)
                self.send_header('Content-type', 'application/json')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                self.wfile.write(json.dumps({'error': 'Could not extract media. Please check if the post is public.'}).encode('utf-8'))

        except Exception as e:
            self.send_response(500)
            self.send_header('Content-type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(json.dumps({'error': str(e)}).encode('utf-8'))

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()
