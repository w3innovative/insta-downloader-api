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
                if media_type in ['reel', 'reels']:
                    clean_url = f'https://www.instagram.com/reel/{code}/'
                    api_endpoint = f'https://www.instagram.com/p/{code}/embed/captioned/'
                else:
                    clean_url = f'https://www.instagram.com/p/{code}/'
                    api_endpoint = f'https://www.instagram.com/p/{code}/embed/captioned/'
            else:
                api_endpoint = clean_url

            media_urls = []
            is_image = True

            # Use Instagram's lightweight embed/shared-data query
            try:
                req = urllib.request.Request(
                    api_endpoint,
                    headers={
                        'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1',
                        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                        'Accept-Language': 'en-US,en;q=0.5'
                    }
                )
                with urllib.request.urlopen(req) as resp:
                    html_content = resp.read().decode('utf-8', errors='ignore')

                    # Check for video streams first
                    vid_matches = re.findall(r'src="([^"]+\.mp4[^"]*)"', html_content)
                    if not vid_matches:
                        vid_matches = re.findall(r'"video_url"\s*:\s*"([^"]+)"', html_content)

                    if vid_matches:
                        vid_url = vid_matches[0].encode().decode('unicode-escape').replace('\\/', '/').replace('&amp;', '&')
                        media_urls.append(vid_url)
                        is_image = False
                    else:
                        # Grab all carousel or single image CDN elements from the page markup
                        matches = re.findall(r'src="([^"]+fbcdn\.net[^"]+)"', html_content)
                        for m in matches:
                            decoded = m.encode().decode('unicode-escape').replace('&amp;', '&')
                            # Strip out square cropping and resizing query constraints
                            decoded = re.sub(r'/s\d+x\d+/', '/', decoded)
                            decoded = re.sub(r'/p\d+x\d+/', '/', decoded)
                            decoded = re.sub(r'/c\d+\.\d+\.\d+\.\d+/', '/', decoded)
                            if 's150x150' not in decoded and 's320x320' not in decoded and decoded not in media_urls:
                                media_urls.append(decoded)
            except Exception as ex:
                pass

            # Fallback to direct page scraping if embed didn't return media
            if not media_urls:
                try:
                    req_direct = urllib.request.Request(
                        clean_url,
                        headers={
                            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
                            'Accept': 'text/html,xhtml+xml'
                        }
                    )
                    with urllib.request.urlopen(req_direct) as resp_direct:
                        html_direct = resp_direct.read().decode('utf-8', errors='ignore')
                        og_match = re.search(r'<meta property="og:image" content="([^"]+)"', html_direct)
                        if og_match:
                            og_url = og_match.group(1).replace('&amp;', '&')
                            og_url = re.sub(r'/s\d+x\d+/', '/', og_url)
                            og_url = re.sub(r'/c\d+\.\d+\.\d+\.\d+/', '/', og_url)
                            media_urls.append(og_url)
                except Exception as ex2:
                    pass

            if media_urls:
                self.send_response(200)
                self.send_header('Content-type', 'application/json')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                self.wfile.write(json.dumps({'download_urls': media_urls, 'is_image': is_image}).encode('utf-8'))
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
