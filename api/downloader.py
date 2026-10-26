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

            media_urls = []
            is_image = True

            # 1. Try yt-dlp first
            ydl_opts = {
                'quiet': True,
                'no_warnings': True,
                'skip_download': True,
            }

            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                try:
                    info = ydl.extract_info(clean_url, download=False)
                    if info:
                        if info.get('ext') in ['mp4', 'webm'] or info.get('vcodec') != 'none':
                            vid_url = info.get('url')
                            if vid_url:
                                media_urls.append(vid_url)
                                is_image = False

                        if 'entries' in info:
                            for entry in info['entries']:
                                node_url = entry.get('url') or entry.get('thumbnail')
                                if node_url and node_url not in media_urls:
                                    media_urls.append(node_url)

                        if not media_urls:
                            if info.get('url'):
                                media_urls.append(info.get('url'))
                            elif info.get('thumbnail'):
                                media_urls.append(info.get('thumbnail'))
                except Exception as ydl_ex:
                    pass

            # 2. Universal Permissive Fallback Scraper (Captures any fbcdn.net media link from page HTML)
            if not media_urls:
                try:
                    req = urllib.request.Request(
                        clean_url,
                        headers={
                            'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1',
                            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                            'Accept-Language': 'en-US,en;q=0.5'
                        }
                    )
                    with urllib.request.urlopen(req) as resp:
                        html_content = resp.read().decode('utf-8', errors='ignore')
                        
                        # Check for video streams
                        vid_matches = re.findall(r'"video_url"\s*:\s*"([^"]+)"', html_content)
                        if vid_matches:
                            media_urls.append(vid_matches[0].encode().decode('unicode-escape').replace('\\/', '/'))
                            is_image = False
                        else:
                            # Capture all high-res image CDN links in the page payload
                            raw_links = re.findall(r'https?://[^\s<>"]+fbcdn\.net[^\s<>"]+', html_content)
                            for link in raw_links:
                                clean_link = link.encode().decode('unicode-escape').replace('\\/', '').replace('&amp;', '&')
                                # Filter out small UI thumbnails and profile avatars
                                if any(ext in clean_link.lower() for ext in ['.jpg', '.jpeg', '.webp']) \
                                   and 's150x150' not in clean_link \
                                   and 's320x320' not in clean_link \
                                   and 'p150x150' not in clean_link \
                                   and clean_link not in media_urls:
                                    media_urls.append(clean_link)

                        # Ultimate fallback to og:image if list is still empty
                        if not media_urls:
                            og_match = re.search(r'<meta property="og:image" content="([^"]+)"', html_content)
                            if og_match:
                                media_urls.append(og_match.group(1).replace('&amp;', '&'))
                except Exception as fallback_ex:
                    pass

            if media_urls:
                cleaned_urls = []
                for url in media_urls:
                    # Clean up dimensions to ensure full uncropped master size
                    clean_img = re.sub(r'/s\d+x\d+/', '/', url)
                    clean_img = re.sub(r'/p\d+x\d+/', '/', clean_img)
                    clean_img = re.sub(r'/c\d+\.\d+\.\d+\.\d+/', '/', clean_img)
                    if clean_img not in cleaned_urls:
                        cleaned_urls.append(clean_img)

                self.send_response(200)
                self.send_header('Content-type', 'application/json')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                self.wfile.write(json.dumps({'download_urls': cleaned_urls, 'is_image': is_image}).encode('utf-8'))
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
