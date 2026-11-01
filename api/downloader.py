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
            code = None
            is_reel_type = False
            if match_post:
                media_type = match_post.group(1)
                code = match_post.group(2)
                if media_type in ['reel', 'reels', 'tv']:
                    is_reel_type = True
            
            if not code:
                self.send_response(400)
                self.send_header('Content-type', 'application/json')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                self.wfile.write(json.dumps({'error': 'Invalid Instagram Post or Reel URL'}).encode('utf-8'))
                return

            media_urls = []
            is_image = not is_reel_type

            # 1. Try mobile JSON graph endpoint first
            try:
                api_json_url = f'https://www.instagram.com/p/{code}/?__a=1&__d=dis'
                req_json = urllib.request.Request(
                    api_json_url,
                    headers={
                        'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 Instagram 300.0.0.0.0',
                        'Accept': 'application/json',
                        'X-IG-App-ID': '936619743392459'
                    }
                )
                with urllib.request.urlopen(req_json) as resp_j:
                    j_data = json.loads(resp_j.read().decode('utf-8', errors='ignore'))
                    items = j_data.get('items', [])
                    if not items and 'graphql' in j_data:
                        items = [j_data['graphql'].get('shortcode_media', {})]

                    for item in items:
                        if is_reel_type or item.get('is_video') or 'video_url' in item:
                            vid = item.get('video_url')
                            if vid:
                                media_urls.append(vid)
                                is_image = False
                                break
                        
                        if 'edge_sidecar_to_children' in item:
                            for edge in item['edge_sidecar_to_children'].get('edges', []):
                                node = edge.get('node', {})
                                resources = node.get('display_resources', [])
                                if resources:
                                    media_urls.append(resources[-1].get('src'))
                                elif 'display_url' in node:
                                    media_urls.append(node.get('display_url'))
                            is_image = True
                            break
                        elif 'display_resources' in item:
                            res = item['display_resources']
                            if res:
                                media_urls.append(res[-1].get('src'))
                            is_image = True
                            break
                        elif 'display_url' in item:
                            media_urls.append(item.get('display_url'))
                            is_image = True
                            break
            except Exception as j_ex:
                pass

            # 2. Fallback to direct page scraping for high-res video or uncropped master images
            if not media_urls:
                try:
                    direct_url = f'https://www.instagram.com/p/{code}/'
                    req_dir = urllib.request.Request(
                        direct_url,
                        headers={
                            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
                            'Accept': 'text/html,application/xhtml+xml'
                        }
                    )
                    with urllib.request.urlopen(req_dir) as resp_dir:
                        html_dir = resp_dir.read().decode('utf-8', errors='ignore')
                        
                        if is_reel_type:
                            og_vid = re.search(r'<meta property="og:video" content="([^"]+)"', html_dir)
                            if og_vid:
                                media_urls.append(og_vid.group(1).replace('&amp;', '&'))
                                is_image = False
                            else:
                                vid_match = re.findall(r'"video_url"\s*:\s*"([^"]+)"', html_dir)
                                if vid_match:
                                    media_urls.append(vid_match[0].encode().decode('unicode-escape').replace('\\/', '/'))
                                    is_image = False
                        else:
                            # Search for display_url nodes which hold full master uncropped dimensions
                            display_urls = re.findall(r'"display_url"\s*:\s*"([^"]+)"', html_dir)
                            for u in display_urls:
                                decoded = u.encode().decode('unicode-escape').replace('\\/', '/')
                                if decoded not in media_urls:
                                    media_urls.append(decoded)
                            
                            if not media_urls:
                                og_img = re.search(r'<meta property="og:image" content="([^"]+)"', html_dir)
                                if og_img:
                                    media_urls.append(og_img.group(1).replace('&amp;', '&'))
                            is_image = True
                except Exception as dir_ex:
                    pass

            if media_urls:
                cleaned_urls = []
                for url in media_urls:
                    if is_image:
                        # Strip downscaling and crop parameters to force original vertical aspect ratio
                        clean_img = re.sub(r'/s\d+x\d+/', '/', url)
                        clean_img = re.sub(r'/p\d+x\d+/', '/', clean_img)
                        clean_img = re.sub(r'/c\d+\.\d+\.\d+\.\d+/', '/', clean_img)
                        if clean_img not in cleaned_urls:
                            cleaned_urls.append(clean_img)
                    else:
                        if url not in cleaned_urls:
                            cleaned_urls.append(url)

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
