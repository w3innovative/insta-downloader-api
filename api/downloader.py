from http.server import BaseHTTPRequestHandler
import base64
import json
import os
import re
import html as htmllib
import urllib.error
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


def fetch_page(url):
    """Returns (status_code, final_url, text). Never raises."""
    req = urllib.request.Request(
        url, headers={'User-Agent': UA, 'Accept-Language': 'en-US,en;q=0.9'}
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, r.geturl(), r.read().decode('utf-8', errors='ignore')
    except urllib.error.HTTPError as e:
        return e.code, url, ''
    except Exception as e:
        return 0, url, str(e)


def normalize(text):
    """Undo JSON / HTML escaping so URLs can be matched with simple regexes."""
    # The JSON can be escaped once or twice (JSON inside a JS string), so each escape may be
    # preceded by several backslashes. Leftover backslashes would cut URLs short, which drops
    # the signature (oh=/oe=) at the end and makes Instagram answer 403.
    text = re.sub(r'\\+u([0-9a-fA-F]{4})', lambda m: chr(int(m.group(1), 16)), text)
    text = re.sub(r'\\+/', '/', text)
    text = re.sub(r'\\+"', '"', text)
    return htmllib.unescape(text)


def url_score(u, w=None, h=None):
    """Rank an Instagram CDN image URL: uncropped beats square-cropped, then more pixels.
    The URL is never modified (the CDN signature would break); we only choose between variants."""
    query = urllib.parse.parse_qs(urllib.parse.urlparse(u).query)
    stp = query.get('stp', [''])[0]
    signed = 1 if ('oh' in query and 'oe' in query) else 0  # unsigned/truncated links give 403
    cropped = bool(re.search(r'(?:^|_)c\d+\.\d+\.\d+\.\d+a?(?:_|$)', stp))
    if w and h:
        pixels = int(w) * int(h)
    else:
        m = re.search(r'(?:^|_)[sp](\d+)x(\d+)(?:_|$)', stp)
        pixels = int(m.group(1)) * int(m.group(2)) if m else 10 ** 7  # no size marker = full size
    return (signed, 0 if cropped else 1, pixels)


def probe(url):
    """HTTP status Instagram's CDN gives for this image link (checked from this server)."""
    try:
        req = urllib.request.Request(url, headers={'User-Agent': UA, 'Range': 'bytes=0-0'})
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:
        return 0


def describe_url(u):
    stp = urllib.parse.parse_qs(urllib.parse.urlparse(u or '').query).get('stp', [''])[0]
    return stp[:70] or 'no-stp'


CDN_URL_RE = re.compile(
    r'https?://[^\s"\'<>\\]+?(?:cdninstagram\.com|fbcdn\.net)[^\s"\'<>\\]*'
)


def find_post_images(text):
    """Pull post-image CDN URLs out of a page. Profile pictures (-19) are skipped."""
    text = normalize(text)
    found = []
    for u in re.findall(r'"display_url"\s*:\s*"(https?://[^"]+)"', text):
        found.append(u)
    for u in CDN_URL_RE.findall(text):
        if re.search(r'-15/', u):  # post media; -19 is profile pics
            found.append(u)

    # Same image appears in several sizes / crops: keep the best (largest, uncropped) variant
    best, order = {}, []
    for u in found:
        key = urllib.parse.urlparse(u).path.rsplit('/', 1)[-1]
        if not key:
            continue
        if key not in best:
            best[key] = u
            order.append(key)
        elif url_score(u) > url_score(best[key]):
            best[key] = u
    return [best[k] for k in order]


def extract_images_fallback(code):
    """Best-effort image extraction for public photo / carousel posts.
    yt-dlp only supports posts that contain video, so photo posts use this.
    Returns (image_urls, diagnostics_string)."""
    images = []
    diag = []

    sources = [
        ('embed-captioned', f'https://www.instagram.com/p/{code}/embed/captioned/'),
        ('embed', f'https://www.instagram.com/p/{code}/embed/'),
        ('page', f'https://www.instagram.com/p/{code}/'),
    ]

    for name, url in sources:
        status, final_url, text = fetch_page(url)
        login_wall = 'accounts/login' in final_url
        found = []
        if status == 200 and not login_wall:
            # carousel images of THIS post, or just the first image (never related posts)
            found = find_sidecar_in_text(text, code) or find_post_images(text)[:1]

        # og:image as a last resort (single image)
        if not found and status == 200 and not login_wall:
            m = (re.search(r'property="og:image"[^>]*content="([^"]+)"', text)
                 or re.search(r'content="([^"]+)"[^>]*property="og:image"', text))
            if m:
                found = [htmllib.unescape(m.group(1))]

        diag.append(f'{name}: http={status} len={len(text)} login={login_wall} images={len(found)}')

        for u in found:
            if u not in images:
                images.append(u)
        if images:
            break

    return images, ' | '.join(diag)


class CaptureLogger:
    """Collects yt-dlp's console messages (used to read its page dumps)."""

    def __init__(self):
        self.lines = []

    def debug(self, msg):
        self.lines.append(str(msg))

    def info(self, msg):
        self.lines.append(str(msg))

    def warning(self, msg):
        pass

    def error(self, msg):
        pass


def _walk(obj):
    """Yield every dict inside a parsed JSON structure."""
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk(v)


def _best_candidate(d):
    """Best (largest, uncropped) image URL among every variant listed on a media node."""
    variants = []  # (url, width, height)
    if d.get('display_url'):
        variants.append((d['display_url'], None, None))
    for c in (d.get('image_versions2') or {}).get('candidates') or []:
        if isinstance(c, dict) and c.get('url'):
            variants.append((c['url'], c.get('width'), c.get('height')))
    for r in d.get('display_resources') or []:
        if isinstance(r, dict) and r.get('src'):
            variants.append((r['src'], r.get('config_width'), r.get('config_height')))
    if not variants:
        return None
    return max(variants, key=lambda v: url_score(*v))[0]


def _media_from_node(m):
    """Image URLs belonging to ONE post node (single photo or carousel)."""
    urls = []
    carousel = m.get('carousel_media')
    if isinstance(carousel, list) and carousel:
        for item in carousel:
            u = _best_candidate(item)
            if u:
                urls.append(u)
        return urls
    edges = (m.get('edge_sidecar_to_children') or {}).get('edges') or []
    if edges:
        for e in edges:
            n = e.get('node') or {}
            u = _best_candidate(n)
            if u:
                urls.append(u)
        return urls
    u = _best_candidate(m)
    return [u] if u else []


def find_media_for_code(obj, code):
    """Find the node whose shortcode matches THIS post and return only its images.
    This stops related / suggested posts in the same response from leaking in."""
    best = []
    for d in _walk(obj):
        if code in (d.get('shortcode'), d.get('code')) and any(
            k in d for k in ('display_url', 'image_versions2', 'carousel_media', 'edge_sidecar_to_children')
        ):
            urls = _media_from_node(d)
            if len(urls) > len(best):  # the full post node has more images than a stub of it
                best = urls
    return best


def _balanced(text, start):
    """Return the JSON object/array that opens at text[start] (string-aware brace matching)."""
    close = '}' if text[start] == '{' else ']'
    depth, in_str, esc = 0, False, False
    for i in range(start, len(text)):
        c = text[i]
        if in_str:
            if esc:
                esc = False
            elif c == '\\':
                esc = True
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c in '{[':
            depth += 1
        elif c in '}]':
            depth -= 1
            if depth == 0:
                return text[start:i + 1] if c == close else None
    return None


def find_sidecar_in_text(text, code):
    """Carousel images from an HTML page: find the 'children' block that sits closest to
    this post's shortcode, so suggested/related carousels on the page are not mixed in."""
    text = normalize(text)
    anchors = [m.start() for m in re.finditer(r'"(?:shortcode|code)"\s*:\s*"' + re.escape(code) + '"', text)]
    if not anchors:
        return []

    candidates = []  # (distance_to_shortcode, urls)
    for key, opener in (('"edge_sidecar_to_children"', '{'), ('"carousel_media"', '[')):
        for m in re.finditer(key, text):
            start = text.find(opener, m.end())
            if start == -1 or text[m.end():start].strip() != ':':
                continue
            blob = _balanced(text, start)
            if not blob:
                continue
            try:
                data = json.loads(blob)
            except Exception:
                continue
            items = ([e.get('node') or {} for e in data.get('edges', [])]
                     if isinstance(data, dict) else data)
            urls = [u for u in (_best_candidate(i) for i in items if isinstance(i, dict)) if u]
            if len(urls) > 1:
                dist = min(abs(m.start() - a) for a in anchors)
                candidates.append((dist, urls))

    if not candidates:
        return []
    return min(candidates, key=lambda c: c[0])[1]


def extract_images_via_ytdlp_dump(url, post_code):
    """yt-dlp reaches Instagram fine from this server but refuses photo-only posts
    ("There is no video in this post"). Re-run it with page dumping on, capture
    every page/JSON response it downloaded, and read the image URLs from those.
    Returns (image_urls, diagnostics_string)."""
    logger = CaptureLogger()
    opts = {
        'quiet': True,
        'no_warnings': True,
        'skip_download': True,
        'dump_intermediate_pages': True,
        'logger': logger,
        'extractor_args': {'instagram': {'max_comments': [0]}},
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.extract_info(url, download=False)
    except Exception:
        pass  # expected: "There is no video in this post"

    pages, total = 0, 0
    json_images = []
    html_sidecar = []
    html_first = []
    for line in logger.lines:
        line = line.strip()
        if len(line) < 200:
            continue
        try:
            text = base64.b64decode(line, validate=True).decode('utf-8', errors='ignore')
        except Exception:
            continue
        pages += 1
        total += len(text)

        if text.lstrip().startswith('{'):
            if not json_images:
                try:
                    json_images = find_media_for_code(json.loads(text), post_code)
                except Exception:
                    pass
        elif post_code and post_code in text:
            if not html_sidecar:
                html_sidecar = find_sidecar_in_text(text, post_code)
            if not html_first:
                html_first = find_post_images(text)[:1]  # single photo: never related posts

    if len(html_sidecar) > len(json_images):
        images, mode = html_sidecar, 'html-carousel'
    elif json_images:
        images, mode = json_images, 'json'
    else:
        images, mode = html_first, 'html-first-only'
    images = list(dict.fromkeys(images))

    return images, (f'ytdlp-dump: lines={len(logger.lines)} pages={pages} bytes={total} '
                    f'mode={mode} images={len(images)} first_stp={describe_url(images[0] if images else None)}')


# ---------------------------------------------------------------------------
# Stories & highlights. Instagram only shows these to logged-in users, so yt-dlp needs
# the cookies of an Instagram account. They are read from Vercel environment variables.
# ---------------------------------------------------------------------------

def cookie_path():
    """Writes the Instagram login cookies to /tmp for yt-dlp.
    Returns the file path, or None when no cookies are configured."""
    raw = os.environ.get('INSTAGRAM_COOKIES_B64', '').strip()
    if raw:
        try:
            text = base64.b64decode(raw).decode('utf-8', errors='ignore')
        except Exception:
            return None
    else:
        text = os.environ.get('INSTAGRAM_COOKIES', '').replace('\\n', '\n').strip()
    if not text.strip():
        return None
    head = text.lstrip()
    if not (head.startswith('# Netscape') or head.startswith('# HTTP Cookie')):
        text = '# Netscape HTTP Cookie File\n' + text
    path = '/tmp/ig_cookies.txt'
    with open(path, 'w') as f:
        f.write(text.rstrip('\n') + '\n')
    return path


def _story_item_lists(obj):
    """Yield every 'items' list that holds story media (not stickers or other nested media)."""
    for d in _walk(obj):
        items = d.get('items')
        if (isinstance(items, list) and items and all(isinstance(i, dict) for i in items)
                and any('image_versions2' in i or 'video_versions' in i for i in items)):
            yield items


def _story_item(d):
    videos = [v for v in (d.get('video_versions') or []) if isinstance(v, dict) and v.get('url')]
    if videos:
        best = max(videos, key=lambda v: (v.get('width') or 0) * (v.get('height') or 0))
        return {'url': best['url'], 'type': 'video'}
    u = _best_candidate(d)
    return {'url': u, 'type': 'image'} if u else None


def collect_story_items(obj):
    out = []
    for items in _story_item_lists(obj):
        for d in items:
            item = _story_item(d)
            if item:
                item['pk'] = str(d.get('pk') or str(d.get('id', '')).split('_')[0])
                out.append(item)
    return out


def story_is_private(obj):
    return any(d.get('is_private') is True and 'username' in d for d in _walk(obj))


IG_APP_ID = '936619743392459'


def _cookie_pairs(path):
    """name -> value for the instagram.com cookies in the cookie file."""
    pairs = {}
    try:
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if line.startswith('#HttpOnly_'):
                    line = line[len('#HttpOnly_'):]
                if not line or line.startswith('#'):
                    continue
                f = line.split('\t')
                if len(f) >= 7 and f[0].lstrip('.').endswith('instagram.com'):
                    pairs[f[5]] = f[6]
    except Exception:
        pass
    return pairs


def ig_api_get(path, cookies):
    """GET an Instagram web-API path with the logged-in cookies. Returns (http_status, body)."""
    pairs = _cookie_pairs(cookies)
    headers = {
        'User-Agent': UA,
        'Accept': '*/*',
        'X-IG-App-ID': IG_APP_ID,
        'X-ASBD-ID': '198387',
        'X-IG-WWW-Claim': '0',
        'X-Requested-With': 'XMLHttpRequest',
        'X-CSRFToken': pairs.get('csrftoken', ''),
        'Origin': 'https://www.instagram.com',
        'Referer': 'https://www.instagram.com/',
        'Cookie': '; '.join(f'{k}={v}' for k, v in pairs.items()),
    }
    req = urllib.request.Request('https://i.instagram.com/api/v1' + path, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, r.read().decode('utf-8', errors='ignore')
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode('utf-8', errors='ignore')
    except Exception as e:
        return 0, str(e)


def _snippet(body, n=160):
    return re.sub(r'\s+', ' ', body or '')[:n]


def story_via_api(who, sid, cookies):
    """Ask Instagram's API directly (same call the website makes).
    Returns (items, diagnostics, is_private). Diagnostics show the exact HTTP answer."""
    if who == 'highlights':
        reel_id = f'highlight:{sid}'
        prefix = ''
    else:
        status, body = ig_api_get('/users/web_profile_info/?username=' + urllib.parse.quote(who), cookies)
        uid = None
        if status == 429:
            # Handle rate limit gracefully by letting yt-dlp handle it or returning diagnostic info
            return [], f'profile http=429 (Rate Limited by Instagram)', False
        try:
            uid = json.loads(body)['data']['user']['id']
        except Exception:
            pass
        if not uid:
            return [], f'profile http={status} body={_snippet(body)}', False
        reel_id = str(uid)
        prefix = f'profile http={status} | '

    status, body = ig_api_get('/feed/reels_media/?reel_ids=' + urllib.parse.quote(reel_id, safe=':'), cookies)
    if status == 429:
        return [], f'{prefix}reels http=429 (Rate Limited)', False
        
    try:
        obj = json.loads(body)
    except Exception:
        return [], f'{prefix}reels http={status} body={_snippet(body)}', False

    if story_is_private(obj):
        return [], f'{prefix}reels http={status} private', True

    items = collect_story_items(obj)
    seen, unique = set(), []
    for it in items:
        k = it['pk'] or it['url']
        if it['url'] and k not in seen:
            seen.add(k)
            unique.append(it)
    diag = f'{prefix}reels http={status} items={len(unique)}'
    if not unique:
        diag += f' body={_snippet(body)}'
    return unique, diag, False


def extract_story(url, story_id):
    """Returns (items, diagnostics, status) where items = [{'url':..., 'type': 'image'|'video'}].
    status is '', 'NO_COOKIES' or 'PRIVATE'."""
    cookies = cookie_path()
    if not cookies:
        return [], 'no cookies configured', 'NO_COOKIES'

    # Step A: ask Instagram's API directly (also reveals the exact reason if it refuses)
    api_diag = ''
    m = re.search(r'/stories/([^/?#]+)/(\d+)', url)
    if m:
        api_items, api_diag, api_private = story_via_api(m.group(1), m.group(2), cookies)
        if api_private:
            return [], api_diag, 'PRIVATE'
        if api_items:
            if story_id:
                only = [it for it in api_items if it['pk'] == story_id]
                if only:
                    api_items = only
            return ([{'url': i['url'], 'type': i['type']} for i in api_items],
                    'story: via=api ' + api_diag, '')

    # Step B: yt-dlp with robust options
    logger = CaptureLogger()
    opts = {
        'quiet': True,
        'no_warnings': True,
        'skip_download': True,
        'cookiefile': cookies,
        'dump_intermediate_pages': True,
        'logger': logger,
        'http_headers': {'User-Agent': UA},
    }
    info, err = None, None
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as e:
        err = str(e)

    items, pages, private = [], 0, False
    for line in logger.lines:
        line = line.strip()
        if len(line) < 200:
            continue
        try:
            text = base64.b64decode(line, validate=True).decode('utf-8', errors='ignore')
        except Exception:
            continue
        pages += 1
        if not text.lstrip().startswith('{'):
            continue
        try:
            obj = json.loads(text)
        except Exception:
            continue
        if story_is_private(obj):
            private = True
        items += collect_story_items(obj)

    if private:
        return [], f'private account (pages={pages})', 'PRIVATE'

    # Same story can appear in several responses: keep each once, in order
    seen, unique = set(), []
    for it in items:
        if it['url'] and (it['pk'] or it['url']) not in seen:
            seen.add(it['pk'] or it['url'])
            unique.append(it)
    items = unique

    # A single story link: return just that story if we can find it
    if story_id:
        only = [it for it in items if it['pk'] == story_id]
        if only:
            items = only

    mode = 'json'
    # Fallback: whatever yt-dlp itself parsed (videos only in most versions)
    if not items and info:
        for entry in (info.get('entries') or [info]):
            if not entry:
                continue
            if entry.get('url') and entry.get('vcodec') not in (None, 'none'):
                items.append({'url': entry['url'], 'type': 'video', 'pk': ''})
            elif entry.get('thumbnail'):
                items.append({'url': entry['thumbnail'], 'type': 'image', 'pk': ''})
        mode = 'ytdlp-entries'

    diag = f'story: api[{api_diag[:300]}] | pages={pages} mode={mode} items={len(items)}'
    if err:
        diag += ' | yt-dlp: ' + err[:120]
    return [{'url': i['url'], 'type': i['type']} for i in items], diag, ''


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

            # ---- Stories and highlights (need the logged-in cookies) ----
            story_m = re.search(r'instagram\.com/stories/([^/?#]+)/?(\d+)?', raw_url)
            if story_m:
                who, sid = story_m.group(1), story_m.group(2)
                if not sid:
                    return self._json(400, {'error': 'Please paste the full story link '
                                            '(instagram.com/stories/username/1234567890/) or a highlight link '
                                            '(instagram.com/stories/highlights/1234567890/).'})
                story_url = f'https://www.instagram.com/stories/{who}/{sid}/'
                items, sdiag, sstatus = extract_story(story_url, None if who == 'highlights' else sid)

                if sstatus == 'NO_COOKIES':
                    return self._json(503, {'error': 'Story downloads are not set up on this server yet.',
                                            'details': sdiag})
                if sstatus == 'PRIVATE':
                    return self._json(403, {'error': 'This story belongs to a private account '
                                                     'and cannot be downloaded.'})
                if items:
                    return self._json(200, {
                        'items': items,
                        'download_urls': [i['url'] for i in items],
                        'is_image': all(i['type'] == 'image' for i in items),
                        'info': sdiag,
                    })
                return self._json(500, {
                    'error': 'Could not extract this story. It may have expired or the account is private. '
                             '[DEBUG: ' + sdiag + ']',
                    'details': sdiag,
                })

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

            is_reel = bool(match_post) and match_post.group(1) in ('reel', 'reels', 'tv')
            media_urls = []
            is_image = True
            last_error = None
            diag = ''

            ydl_opts = {
                'quiet': True,
                'no_warnings': True,
                'skip_download': True,
                'extractor_args': {'instagram': {'max_comments': [0]}},
            }

            # Step 1: yt-dlp (works for reels / video posts)
            info = None
            for attempt in range(2 if is_reel else 1):  # reels: retry once on a flaky response
                try:
                    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                        info = ydl.extract_info(clean_url, download=False)
                    last_error = None
                    break
                except Exception as ydl_ex:
                    last_error = str(ydl_ex)

            try:
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
            # Never used for reels: a failed reel must not turn into photos of other posts.
            if not media_urls and post_code and not is_reel:
                media_urls, diag = extract_images_via_ytdlp_dump(clean_url, post_code)
                is_image = True

            # Step 3: last resort, scrape Instagram's embed pages directly (first image only,
            # so related posts on the page are never returned)
            if not media_urls and post_code and not is_reel:
                found, diag2 = extract_images_fallback(post_code)
                media_urls = found
                diag = (diag + ' | ' if diag else '') + diag2
                is_image = True

            if media_urls:
                cleaned_urls = list(dict.fromkeys(media_urls))  # dedupe, keep order
                info_text = diag
                if is_image:
                    info_text += f' | probe_first={probe(cleaned_urls[0])}'
                return self._json(200, {'download_urls': cleaned_urls, 'is_image': is_image, 'info': info_text})

            details = ''
            if last_error:
                details += 'yt-dlp: ' + last_error[:250]
            if diag:
                details += (' || ' if details else '') + diag[:400]

            # Details go inside the error text so your existing page shows them
            # without any change to functions.php. Remove this once it works.
            self._json(500, {
                'error': 'Could not extract media. Please check if the post is public. [DEBUG: '
                         + (details or 'no details') + ']',
                'details': details,
            })

        except Exception as e:
            self._json(500, {'error': str(e)})

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()
