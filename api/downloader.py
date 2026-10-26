from http.server import BaseHTTPRequestHandler
import json
import urllib.parse
import urllib.request
import re
import yt_dlp

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
                if media_type in ['reel', 'reels']:
                    clean_url = f'https://www.instagram.com/reel/{code}/'
                else:
                    clean_url = f'https://www.instagram.com/p/{code}/'

            media_url = None
            is_image = False

            # Use yt-dlp to extract full uncropped payload structures
            ydl_opts = {
                'quiet': True,
                'no_warnings': True,
                'skip_download': True,
            }

            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                try:
                    info = ydl.extract_info(clean_url, download=False)
                    if info:
                        # 1. Check direct formats or video streams
                        media_url = info.get('url')
                        
                        # 2. Check for carousel/sidecar entries (takes the first or highest res node)
                        if not media_url and 'entries' in info and len(info['entries']) > 0:
                            first_entry = info['entries'][0]
                            media_url = first_entry.get('url') or first_entry.get('thumbnail')
                            if not first_entry.get('url'):
                                is_image = True

                        # 3. Look through all available thumbnails/formats for the maximum height/width ratio (true vertical)
                        candidate_urls = []
                        if 'thumbnails' in info:
                            for thumb in info['thumbnails']:
                                if thumb.get('url'):
                                    candidate_urls.append(thumb.get('url'))
                        if 'formats' in info:
                            for fmt in info['formats']:
                                if fmt.get('url') and not fmt.get('vcodec') == 'none':
                                    candidate_urls.append(fmt.get('url'))

                        if candidate_urls:
                            # Filter out small preview query strings if possible, or pick the last/largest
                            media_url = candidate_urls[-1]
                            is_image = True

                        if not media_url and info.get('thumbnail'):
                            media_url = info.get('thumbnail')
                            is_image = True
                except Exception as ex:
                    pass

            # Deep JSON / SharedData fallback parser if yt-dlp returns cropped meta
            if not media_url or 's640x640' in media_url or '150x150' in media_url:
                try:
                    req = urllib.request.Request(
                        clean_url,
                        headers={'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1'}
                    )
                    with urllib.request.urlopen(req) as resp:
                        html_content = resp.read().decode('utf-8', errors='ignore')
                        
                        # Search for display_url inside shared data script blocks
                        display_urls = re.findall(r'"display_url"\s*:\s*"([^"]+)"', html_content)
                        if display_urls:
                            # Unescape unicode slashes
                            media_url = display_urls[0].encode().decode('unicode-escape').replace('\\/', '/')
                            is_image = True
                        else:
                            # Fallback to og:image if display_url is missing
                            og_image_match = re.search(r'<meta property="og:image" content="([^"]+)"', html_content)
                            if og_image_match:
                                media_url = og_image_match.group(1).replace('&amp;', '&')
                                is_image = True
                except Exception as fallback_ex:
                    pass

            if media_url:
                # Strip all Instagram dimension constraints and cropping parameters to guarantee original full size
                media_url = re.sub(r'/s\d+x\d+/', '/', media_url)
                media_url = re.sub(r'/p\d+x\d+/', '/', media_url)
                media_url = re.sub(r'\?efg=[^&]+', '', media_url) # Clean tracking tokens that force square scaling

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
