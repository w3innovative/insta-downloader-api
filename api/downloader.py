from http.server import BaseHTTPRequestHandler
import base64
import http.cookiejar
import json
import os
import re
import shutil
import subprocess
import tempfile
import html as htmllib
from types import SimpleNamespace
import urllib.error
import urllib.parse
import urllib.request
import yt_dlp

UA = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/124.0 Safari/537.36'
)

# Allowed CDN hosts for Instagram and TikTok media proxying
ALLOWED_PROXY_SUFFIXES = (
    'cdninstagram.com',
    'fbcdn.net',
    'tiktok.com',
    'tiktokcdn.com',
    'tiktokcdn-us.com',
    'tiktokcdn-eu.com',
    'byteoversea.com',
    'ibyteimg.com',
    'ibytedtos.com',
    'muscdn.com',
    'googlevideo.com',
    'ytimg.com'
)


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
    text = re.sub(r'\\+u([0-9a-fA-F]{4})', lambda m: chr(int(m.group(1), 16)), text)
    text = re.sub(r'\\+/', '/', text)
    text = re.sub(r'\\+"', '"', text)
    return htmllib.unescape(text)


def url_score(u, w=None, h=None):
    """Rank an Instagram CDN image URL: uncropped beats square-cropped, then more pixels."""
    query = urllib.parse.parse_qs(urllib.parse.urlparse(u).query)
    stp = query.get('stp', [''])[0]
    signed = 1 if ('oh' in query and 'oe' in query) else 0
    cropped = bool(re.search(r'(?:^|_)c\d+\.\d+\.\d+\.\d+a?(?:_|$)', stp))
    if w and h:
        pixels = int(w) * int(h)
    else:
        m = re.search(r'(?:^|_)[sp](\d+)x(\d+)(?:_|$)', stp)
        pixels = int(m.group(1)) * int(m.group(2)) if m else 10 ** 7
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
        if re.search(r'-15/', u):
            found.append(u)

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
    """Best-effort image extraction for public photo / carousel posts."""
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
            found = find_sidecar_in_text(text, code) or find_post_images(text)[:1]

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
    """Collects yt-dlp's console messages."""

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
    variants = []
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
    best = []
    for d in _walk(obj):
        if code in (d.get('shortcode'), d.get('code')) and any(
            k in d for k in ('display_url', 'image_versions2', 'carousel_media', 'edge_sidecar_to_children')
        ):
            urls = _media_from_node(d)
            if len(urls) > len(best):
                best = urls
    return best


def _balanced(text, start):
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
    text = normalize(text)
    anchors = [m.start() for m in re.finditer(r'"(?:shortcode|code)"\s*:\s*"' + re.escape(code) + '"', text)]
    if not anchors:
        return []

    candidates = []
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
        pass

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
                html_first = find_post_images(text)[:1]

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
# TikTok download headers
# TikTok's media links only work with the cookies / headers that were used to fetch the
# page (e.g. the tt_chain_token cookie). yt-dlp knows them, so they are packed into a small
# token that the page sends back, and the proxy forwards them to TikTok's CDN.
# ---------------------------------------------------------------------------
FORWARDED_HEADERS = ('User-Agent', 'Referer', 'Cookie', 'Accept', 'Accept-Language')


def make_download_token(ydl, media_url, base_headers):
    """Packs the cookies/headers TikTok needs for this media link into a small token."""
    host = (urllib.parse.urlparse(media_url).hostname or '').lower()
    cookies = []
    try:
        for c in ydl.cookiejar:
            d = (c.domain or '').lstrip('.').lower()
            if d and (host == d or host.endswith('.' + d)):
                cookies.append(f'{c.name}={c.value}')
    except Exception:
        pass
    h = {k: v for k, v in (base_headers or {}).items() if k in FORWARDED_HEADERS and isinstance(v, str)}
    h.setdefault('User-Agent', UA)
    h.setdefault('Referer', 'https://www.tiktok.com/')
    if cookies:
        h['Cookie'] = '; '.join(cookies)
    raw = json.dumps(h, separators=(',', ':')).encode('utf-8')
    return base64.urlsafe_b64encode(raw).decode('ascii').rstrip('=')


def sniff_audio(first_bytes):
    """'mp3' for a real MP3 file, 'm4a' for audio inside an MP4 container, else None."""
    b = first_bytes or b''
    if b[:3] == b'ID3' or (len(b) > 1 and b[0] == 0xFF and (b[1] & 0xE0) == 0xE0):
        return 'mp3'
    if len(b) >= 8 and b[4:8] == b'ftyp':
        return 'm4a'
    return None


def proxy_request_headers(target_url, h_param, client_range):
    """Headers the proxy sends to the CDN, plus the token's headers if given."""
    host = (urllib.parse.urlparse(target_url or '').hostname or '').lower()
    headers = {'User-Agent': UA, 'Accept': '*/*', 'Accept-Language': 'en-US,en;q=0.9'}
    if 'tiktok' in host or host.endswith('byteoversea.com'):
        headers['Referer'] = 'https://www.tiktok.com/'
    if h_param:
        try:
            extra = json.loads(base64.urlsafe_b64decode(h_param + '=' * (-len(h_param) % 4)))
            for k in FORWARDED_HEADERS:
                v = extra.get(k)
                if isinstance(v, str) and '\n' not in v and '\r' not in v:
                    headers[k] = v
        except Exception:
            pass
    if client_range:
        headers['Range'] = client_range
    return headers


# ---------------------------------------------------------------------------
# TikTok photo / slideshow posts
# yt-dlp does not support tiktok.com/@user/photo/ID links ("Unsupported URL"), so these are
# read straight from the TikTok page, which contains the post data as JSON.
# ---------------------------------------------------------------------------
def resolve_tiktok_url(url):
    """Short links (vm.tiktok.com, vt.tiktok.com, tiktok.com/t/...) -> the full post link."""
    p = urllib.parse.urlparse(url)
    host = (p.hostname or '').lower()
    if host in ('vm.tiktok.com', 'vt.tiktok.com') or p.path.startswith('/t/'):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': UA})
            with urllib.request.urlopen(req, timeout=15) as r:
                return r.geturl().split('?')[0]
        except Exception:
            pass
    return url


MOBILE_UA = ('Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 '
             '(KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1')


def _tiktok_open(url, ua=UA):
    """Loads a TikTok page and keeps the cookies TikTok sets. Returns (status, html, cookie_jar)."""
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    req = urllib.request.Request(url, headers={
        'User-Agent': ua,
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9',
    })
    with opener.open(req, timeout=20) as r:
        return r.status, r.read().decode('utf-8', errors='ignore'), jar


def _pick_photo_url(url_list):
    """TikTok lists several versions of each photo; prefer a plain JPEG."""
    for u in url_list:
        if re.search(r'jpe?g', u.split('?')[0], re.I):
            return u
    return url_list[0]


def _page_json(page):
    for script_id in ('__UNIVERSAL_DATA_FOR_REHYDRATION__', 'SIGI_STATE'):
        m = re.search(r'<script[^>]+id="' + script_id + r'"[^>]*>(.*?)</script>', page, re.S)
        if m:
            try:
                return json.loads(m.group(1))
            except Exception:
                pass
    return None


def _photo_url_lists(data):
    """One list of URL versions per photo. Looks at imagePost.images first, then at any
    'images' list whose entries carry imageURL.urlList."""
    def urls_of(images):
        out = []
        for img in images:
            ul = ((img.get('imageURL') or {}).get('urlList') or []) if isinstance(img, dict) else []
            ul = [u for u in ul if isinstance(u, str)]
            if ul:
                out.append(ul)
        return out

    for d in _walk(data):
        ip = d.get('imagePost')
        if isinstance(ip, dict) and isinstance(ip.get('images'), list):
            found = urls_of(ip['images'])
            if found:
                return found
    for d in _walk(data):
        imgs = d.get('images')
        if isinstance(imgs, list) and imgs and all(isinstance(i, dict) and isinstance(i.get('imageURL'), dict) for i in imgs):
            found = urls_of(imgs)
            if found:
                return found
    return []


def _photo_meta(data):
    """Caption and music link of the post."""
    for d in _walk(data):
        if 'desc' in d and isinstance(d.get('music'), dict):
            return d.get('desc') or '', d['music'].get('playUrl') or None
    return '', None


def _tiktok_diag(label, status, page, data):
    parts = [f'{label}: http={status}', f'bytes={len(page)}', f"imagePost_in_page={'imagePost' in page}"]
    if 'captcha' in page.lower() or 'verify' in page.lower()[:5000]:
        parts.append('captcha/verify-text')
    if data is None:
        parts.append('no-json')
    elif isinstance(data, dict):
        scope = data.get('__DEFAULT_SCOPE__')
        if isinstance(scope, dict):
            parts.append('scope=' + ','.join(list(scope.keys())[:6]))
            vd = scope.get('webapp.video-detail')
            if isinstance(vd, dict):
                parts.append(f"detail-status={vd.get('statusCode')}/{str(vd.get('statusMsg'))[:30]}")
                struct = (vd.get('itemInfo') or {}).get('itemStruct')
                parts.append('item=' + (','.join(list(struct.keys())[:8]) if isinstance(struct, dict) else 'none'))
        else:
            parts.append('keys=' + ','.join(list(data.keys())[:6]))
    return ' '.join(parts)


def extract_tiktok_photo(url):
    # Try the photo page, the same post as a /video/ link, and a mobile browser view
    attempts = [('photo', url, UA)]
    if '/photo/' in url:
        attempts.append(('video-url', url.replace('/photo/', '/video/'), UA))
    attempts.append(('mobile', url, MOBILE_UA))

    diags = []
    for label, page_url, ua in attempts:
        try:
            status, page, jar = _tiktok_open(page_url, ua)
        except urllib.error.HTTPError as e:
            diags.append(f'{label}: http={e.code}')
            continue
        except Exception as e:
            diags.append(f'{label}: {str(e)[:60]}')
            continue

        data = _page_json(page)
        url_lists = _photo_url_lists(data) if data is not None else []
        if not url_lists:
            diags.append(_tiktok_diag(label, status, page, data))
            continue

        holder = SimpleNamespace(cookiejar=jar)
        base_headers = {'User-Agent': ua, 'Referer': 'https://www.tiktok.com/'}
        items = []
        for ul in url_lists:
            photo = _pick_photo_url(ul)
            items.append({'url': photo, 'type': 'image', 'h': make_download_token(holder, photo, base_headers)})

        desc, audio_url = _photo_meta(data)
        audio_h = make_download_token(holder, audio_url, base_headers) if audio_url else None
        return {
            'items': items,
            'download_urls': [i['url'] for i in items],
            'is_image': True,
            'title': desc,
            'thumbnail': items[0]['url'],
            'audio_url': audio_url,
            'audio_h': audio_h,
        }

    return {'error': 'No photos were found in this TikTok post. [DEBUG: ' + ' || '.join(diags)[:600] + ']'}


# ---------------------------------------------------------------------------
# TikTok Extraction Feature
# ---------------------------------------------------------------------------
def extract_tiktok(url):
    url = resolve_tiktok_url(url)

    # Photo / slideshow posts are not supported by yt-dlp
    if re.search(r'tiktok\.com/@[^/]+/photo/\d+', url):
        return extract_tiktok_photo(url)

    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'skip_download': True,
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)

            title = info.get('title', '')
            thumbnail = info.get('thumbnail', '')
            audio_url, audio_h = None, None
            items = []

            # Photo/slideshow collection or single video
            entries = info.get('entries')
            if entries:
                for entry in entries:
                    if not entry:
                        continue
                    img_url = entry.get('url') or entry.get('thumbnail')
                    if img_url:
                        items.append({
                            'url': img_url,
                            'type': 'image',
                            'h': make_download_token(ydl, img_url, entry.get('http_headers') or info.get('http_headers')),
                        })
            else:
                video_url = info.get('url')
                if video_url:
                    items.append({
                        'url': video_url,
                        'type': 'video',
                        'h': make_download_token(ydl, video_url, info.get('http_headers')),
                    })
                elif thumbnail:
                    items.append({
                        'url': thumbnail,
                        'type': 'image',
                        'h': make_download_token(ydl, thumbnail, info.get('http_headers')),
                    })

            # Background music / audio track if available
            for f in info.get('formats') or []:
                if f.get('acodec') not in (None, 'none') and f.get('vcodec') == 'none' and f.get('url'):
                    audio_url = f['url']
                    audio_h = make_download_token(ydl, audio_url, f.get('http_headers') or info.get('http_headers'))
                    break

            if not items:
                return {'error': 'Could not extract media links from TikTok URL.'}

            return {
                'items': items,
                'download_urls': [i['url'] for i in items],
                'is_image': all(i['type'] == 'image' for i in items),
                'title': title,
                'thumbnail': thumbnail,
                'audio_url': audio_url,
                'audio_h': audio_h,
            }
    except Exception as e:
        err = 'TikTok extraction error: ' + str(e)[:200]
        # A "video" link can also be a photo slideshow: try reading it as one
        photo = extract_tiktok_photo(url)
        if 'error' not in photo:
            return photo
        return {'error': err + ' | photo: ' + photo['error']}


# ---------------------------------------------------------------------------
# Facebook (videos, reels, watch, fb.watch links, photos)
# Videos and reels go through yt-dlp. Photos (and anything yt-dlp cannot read) are read from the
# Open Graph tags Facebook shows to its own link-preview crawler.
# Stories need a logged-in account, so they are not supported.
# ---------------------------------------------------------------------------
FB_UA = 'facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)'


def is_facebook_url(raw_url):
    try:
        p = urllib.parse.urlparse(raw_url if '://' in raw_url else 'https://' + raw_url)
        host = (p.hostname or '').lower()
    except Exception:
        return False
    return (host in ('fb.watch', 'fb.com', 'facebook.com')
            or host.endswith('.facebook.com') or host.endswith('.fb.com'))


def resolve_fb_url(url):
    """Short and share links (fb.watch/..., facebook.com/share/...) -> the real post link."""
    p = urllib.parse.urlparse(url)
    host = (p.hostname or '').lower()
    if host == 'fb.watch' or '/share/' in p.path:
        try:
            req = urllib.request.Request(url, headers={'User-Agent': UA, 'Accept-Language': 'en-US,en;q=0.9'})
            with urllib.request.urlopen(req, timeout=15) as r:
                final = r.geturl()
        except Exception:
            return url
        # A login page keeps the real link in its "next" parameter
        nxt = urllib.parse.parse_qs(urllib.parse.urlparse(final).query).get('next')
        if '/login' in final and nxt:
            return nxt[0]
        return final
    return url


def fb_is_photo(url):
    p = urllib.parse.urlparse(url)
    path, query = p.path.lower(), p.query.lower()
    if any(s in path for s in ('/videos/', '/video/', '/reel/', '/reels/', '/watch')):
        return False
    return path.startswith('/photo') or '/photos/' in path or 'fbid=' in query


def fb_fetch(url, ua):
    """Returns (status, final_url, html). Never raises."""
    req = urllib.request.Request(url, headers={
        'User-Agent': ua,
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9',
    })
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, r.geturl(), r.read().decode('utf-8', errors='ignore')
    except urllib.error.HTTPError as e:
        return e.code, url, ''
    except Exception as e:
        return 0, url, ''


def _og_values(page, prop):
    vals = []
    for m in re.finditer(r'<meta\s[^>]*>', page, re.I):
        tag = m.group(0)
        if not re.search(r'(?:property|name)\s*=\s*["\']' + re.escape(prop) + r'["\']', tag, re.I):
            continue
        cm = re.search(r'content\s*=\s*"([^"]*)"', tag, re.I) or re.search(r"content\s*=\s*'([^']*)'", tag, re.I)
        if cm:
            vals.append(htmllib.unescape(cm.group(1)))
    return vals


def _fb_result(items, title='', thumbnail='', qualities=None, audio_url=None, duration=None):
    return {
        'items': items,
        'download_urls': [i['url'] for i in items],
        'is_image': all(i['type'] == 'image' for i in items),
        'title': title or '',
        'thumbnail': thumbnail or '',
        'qualities': qualities or [],
        'audio_url': audio_url,
        'duration': duration,
    }


def fb_pick_formats(info):
    """Playable files from yt-dlp's format list.
    Returns (progressive, audio_url): progressive = [(label, url)] best first, files that already
    contain picture + sound. Separate video-only / audio-only streams cannot be merged on Vercel."""
    prog, audio = [], []
    for f in info.get('formats') or []:
        url = f.get('url')
        if not url:
            continue
        protocol = str(f.get('protocol') or '')
        if 'm3u8' in protocol or 'dash' in protocol or 'f4m' in protocol:
            continue
        vcodec, acodec = f.get('vcodec'), f.get('acodec')
        if vcodec == 'none' and acodec != 'none':
            audio.append((f.get('abr') or f.get('tbr') or 0, url))
        elif vcodec != 'none' and acodec != 'none':
            prog.append((f.get('height') or 0, f.get('tbr') or 0, f.get('format_id') or '', url))
    prog.sort(key=lambda x: (x[0], x[1]), reverse=True)

    out, seen = [], set()
    for height, _tbr, fid, url in prog:
        if url in seen:
            continue
        seen.add(url)
        tag = 'HD' if fid == 'hd' else ('SD' if fid == 'sd' else '')
        label = ' '.join(x for x in (tag, f'{height}p' if height else '') if x) or fid or 'Video'
        out.append((label, url))
    audio.sort(reverse=True)
    return out, (audio[0][1] if audio else None)


def fb_video_via_ytdlp(url):
    opts = {'quiet': True, 'no_warnings': True, 'skip_download': True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    if not info:
        return None

    entries = [e for e in (info.get('entries') or []) if e] or [info]
    items, qualities, audio_url = [], [], None
    for n, entry in enumerate(entries):
        prog, audio = fb_pick_formats(entry)
        best = prog[0][1] if prog else entry.get('url')
        if not best:
            continue
        items.append({'url': best, 'type': 'video'})
        if n == 0:
            qualities = [{'label': label, 'url': u} for label, u in prog] or [{'label': 'Video', 'url': best}]
            audio_url = audio
    if not items:
        return None

    first = entries[0]
    title = (first.get('title') or first.get('description') or '').strip()
    return _fb_result(items, title[:150], first.get('thumbnail'), qualities, audio_url, first.get('duration'))


def fb_open_graph_media(url):
    """Photos / videos from the preview tags Facebook shows to its own crawler."""
    status, final, page = fb_fetch(url, FB_UA)
    if status != 200 or not page:
        return {'diag': f'og: http={status}'}
    if '/login' in final and 'next=' in final:
        return {'diag': 'og: login wall'}

    title = (_og_values(page, 'og:title') or [''])[0]
    thumb = (_og_values(page, 'og:image') or [''])[0]

    videos = []
    for prop in ('og:video:secure_url', 'og:video:url', 'og:video'):
        for v in _og_values(page, prop):
            host = (urllib.parse.urlparse(v).hostname or '').lower()
            if v.startswith('http') and (host.endswith('fbcdn.net') or urllib.parse.urlparse(v).path.lower().endswith('.mp4')):
                videos.append(v)
    if videos:
        return _fb_result([{'url': videos[0], 'type': 'video'}], title, thumb,
                          [{'label': 'Video', 'url': videos[0]}])

    images = []
    for v in _og_values(page, 'og:image'):
        host = (urllib.parse.urlparse(v).hostname or '').lower()
        if host.endswith('fbcdn.net') and '/rsrc.php' not in v and v not in images:
            images.append(v)
    if images:
        return _fb_result([{'url': u, 'type': 'image'} for u in images], title, images[0])

    return {'diag': f'og: http={status} bytes={len(page)} images=0 video=0'}


def _fb_page_photos(page):
    """Photo links inside an embed / mobile page (profile pictures, icons and tiny thumbnails skipped)."""
    text = normalize(page)
    out = []
    for u in re.findall(r'https?://[^\s"\'<>\\]+', text):
        host = (urllib.parse.urlparse(u).hostname or '').lower()
        if not (host.endswith('fbcdn.net') or host.startswith('scontent')):
            continue
        if re.search(r'/rsrc\.php|emoji|static\.|/safe_image|[/_]p\d{2,3}x\d{2,3}|s\d{2,3}x\d{2,3}|\.30808-1/', u):
            continue
        if not re.search(r'\.(?:jpg|jpeg|png|webp)(?:\?|$)', urllib.parse.urlparse(u).path + '?', re.I):
            continue
        if u not in out:
            out.append(u)
    return out


def fb_photo_media(url):
    """Photos: several ways of asking Facebook, because it shows a login page to some of them."""
    p = urllib.parse.urlparse(url)
    q = urllib.parse.parse_qs(p.query)
    fbid = (q.get('fbid') or [''])[0]
    if not fbid:
        m = re.search(r'/(?:photos?|posts|permalink)[^?]*?/(\d{8,})', p.path)
        fbid = m.group(1) if m else ''
    canonical = f'https://www.facebook.com/photo.php?fbid={fbid}' if fbid else ''
    enc = urllib.parse.quote(url, safe='')

    attempts = [('og', url, FB_UA, 'og')]
    if fbid:
        attempts.append(('og-m', f'https://m.facebook.com/photo.php?fbid={fbid}', FB_UA, 'og'))
        attempts.append(('og-canon', canonical, FB_UA, 'og'))
    for bot, bua in (('whatsapp', 'WhatsApp/2.23.20.0 A'), ('twitter', 'Twitterbot/1.0'),
                     ('slack', 'Slackbot-LinkExpanding 1.0 (+https://api.slack.com/robots)'),
                     ('telegram', 'TelegramBot (like TwitterBot)'),
                     ('google', 'Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)')):
        attempts.append((bot, canonical or url, bua, 'og'))
        attempts.append((bot + '-m', f'https://m.facebook.com/photo.php?fbid={fbid}' if fbid else url, bua, 'og'))
    attempts.append(('embed', f'https://www.facebook.com/plugins/post.php?href={enc}&show_text=true&width=500', UA, 'page'))
    if canonical:
        enc2 = urllib.parse.quote(canonical, safe='')
        attempts.append(('embed2', f'https://www.facebook.com/plugins/post.php?href={enc2}&show_text=true&width=500', UA, 'page'))
    if fbid:
        attempts.append(('mbasic', f'https://mbasic.facebook.com/photo.php?fbid={fbid}', UA, 'page'))

    diag = []
    for name, target, ua, mode in attempts:
        status, final, page = fb_fetch(target, ua)
        wall = '/login' in final or 'login.php' in final
        if status != 200 or not page or wall:
            diag.append(f'{name}: http={status}{" login" if wall else ""}')
            continue
        title = (_og_values(page, 'og:title') or [''])[0]
        images = []
        for v in _og_values(page, 'og:image'):
            host = (urllib.parse.urlparse(v).hostname or '').lower()
            if host.endswith('fbcdn.net') and '/rsrc.php' not in v and v not in images:
                images.append(v)
        if not images and mode == 'page':
            images = _fb_page_photos(page)[:12]
        diag.append(f'{name}: http={status} images={len(images)}')
        if images:
            return _fb_result([{'url': u, 'type': 'image'} for u in images], title, images[0])
    return {'diag': 'photo: ' + '; '.join(diag)}


def extract_facebook(raw_url):
    url = resolve_fb_url(raw_url.strip())

    if '/stories/' in urllib.parse.urlparse(url).path.lower():
        return {'error': 'Facebook stories can only be viewed when logged in, so they cannot be downloaded here.'}

    notes = []
    if not fb_is_photo(url):
        try:
            result = fb_video_via_ytdlp(url)
            if result:
                return result
            notes.append('yt-dlp: no playable video')
        except Exception as e:
            notes.append('yt-dlp: ' + re.sub(r'\s+', ' ', str(e))[:180])

    og = fb_photo_media(url) if fb_is_photo(url) else fb_open_graph_media(url)
    if og.get('items'):
        return og
    if og.get('diag'):
        notes.append(og['diag'])

    joined = ' | '.join(notes)
    if re.search(r'log ?in|private|not available|permission', joined, re.I):
        msg = 'This Facebook content looks private or needs a login, so it cannot be downloaded.'
    else:
        msg = 'Could not get this Facebook content. It may be private or deleted, or Facebook may be blocking the request.'
    return {'error': msg + ' [DEBUG: ' + joined[:400] + ']'}


# ---------------------------------------------------------------------------
# YouTube
# Videos / Shorts / audio come from yt-dlp (only ready-made files that already contain picture and
# sound, because Vercel has no room to merge separate streams). Thumbnails need no yt-dlp at all.
# ---------------------------------------------------------------------------
def is_youtube_url(raw_url):
    try:
        p = urllib.parse.urlparse(raw_url if '://' in raw_url else 'https://' + raw_url)
        host = (p.hostname or '').lower()
    except Exception:
        return False
    return any(host == d or host.endswith('.' + d) for d in ('youtube.com', 'youtu.be', 'youtube-nocookie.com'))


def yt_video_id(raw_url):
    p = urllib.parse.urlparse(raw_url if '://' in raw_url else 'https://' + raw_url)
    host = (p.hostname or '').lower()
    if host == 'youtu.be':
        m = re.match(r'/([\w\-]{11})', p.path)
        return m.group(1) if m else None
    m = re.match(r'/(?:shorts|live|embed|v)/([\w\-]{11})', p.path)
    if m:
        return m.group(1)
    v = urllib.parse.parse_qs(p.query).get('v', [''])[0]
    return v if re.fullmatch(r'[\w\-]{11}', v or '') else None


def yt_head_ok(url):
    try:
        req = urllib.request.Request(url, headers={'User-Agent': UA}, method='HEAD')
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status == 200
    except Exception:
        return False


def yt_oembed_title(vid):
    try:
        u = 'https://www.youtube.com/oembed?format=json&url=' + urllib.parse.quote(
            f'https://www.youtube.com/watch?v={vid}', safe='')
        req = urllib.request.Request(u, headers={'User-Agent': UA})
        with urllib.request.urlopen(req, timeout=10) as r:
            return (json.loads(r.read().decode('utf-8', errors='ignore')).get('title') or '').strip()
    except Exception:
        return ''


def yt_thumbnails(vid):
    """Thumbnail sizes that exist for this video, largest first."""
    sizes = [('maxresdefault', 'Max quality (1280x720)'), ('sddefault', 'HD (640x480)'),
             ('hqdefault', 'Standard (480x360)')]
    out = []
    for name, label in sizes:
        u = f'https://i.ytimg.com/vi/{vid}/{name}.jpg'
        if yt_head_ok(u):
            out.append({'label': label, 'url': u})
    return out


def yt_token(headers):
    h = {k: v for k, v in (headers or {}).items() if k in FORWARDED_HEADERS and isinstance(v, str)}
    h.setdefault('User-Agent', UA)
    h['Referer'] = 'https://www.youtube.com/'
    h.pop('Cookie', None)
    raw = json.dumps(h, separators=(',', ':')).encode('utf-8')
    return base64.urlsafe_b64encode(raw).decode('ascii').rstrip('=')


def yt_pick_formats(info):
    """(qualities, audio_url, headers). qualities = [{'label','url','size'}] tallest first."""
    prog, audio, headers = {}, [], None
    for f in info.get('formats') or []:
        url = f.get('url')
        if not url or str(f.get('protocol') or '') not in ('https', 'http'):
            continue
        v, a = f.get('vcodec'), f.get('acodec')
        height = f.get('height') or 0
        size = f.get('filesize') or f.get('filesize_approx') or 0
        if v not in (None, 'none') and a not in (None, 'none'):
            if f.get('ext') != 'mp4':
                continue
            cur = prog.get(height)
            if not cur or (f.get('tbr') or 0) > cur['tbr']:
                prog[height] = {'url': url, 'tbr': f.get('tbr') or 0, 'size': size}
                headers = headers or f.get('http_headers')
        elif v in (None, 'none') and a not in (None, 'none'):
            audio.append((1 if f.get('ext') == 'm4a' else 0, f.get('abr') or f.get('tbr') or 0, url))
            headers = headers or f.get('http_headers')
    qualities = [{'label': f'{h}p' if h else 'Video', 'url': d['url'], 'size': d['size']}
                 for h, d in sorted(prog.items(), reverse=True)]
    audio.sort(reverse=True)
    return qualities, (audio[0][2] if audio else None), (headers or info.get('http_headers'))


def extract_youtube(raw_url, mode='video'):
    vid = yt_video_id(raw_url)
    if not vid:
        return {'error': 'Could not find a video in this link. Please paste a full YouTube video or Shorts link.'}

    title = yt_oembed_title(vid)
    thumbs = yt_thumbnails(vid)

    if mode == 'thumbnail':
        if not thumbs:
            return {'error': 'No thumbnail was found for this video. It may be private or deleted.'}
        return {'video_id': vid, 'title': title, 'thumbnail': thumbs[0]['url'], 'thumbnails': thumbs,
                'download_urls': [t['url'] for t in thumbs], 'is_image': True}

    if cobalt_instances():
        # Cobalt makes the files; here only the preview info is needed (works even when YouTube blocks yt-dlp)
        return {
            'video_id': vid, 'title': title[:150], 'thumbnail': (thumbs[0]['url'] if thumbs else ''),
            'thumbnails': thumbs, 'cobalt': True, 'download_urls': [], 'is_image': False,
            'qualities': [{'label': (q + 'p'), 'q': q} for q in ('1080', '720', '480', '360')],
            'audio': True,
        }

    attempts = [None, ['android_vr'], ['tv'], ['mweb']]
    notes, info, found = [], None, None
    for clients in attempts:
        opts = {'quiet': True, 'no_warnings': True, 'skip_download': True, 'noplaylist': True}
        if clients:
            opts['extractor_args'] = {'youtube': {'player_client': clients}}
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(f'https://www.youtube.com/watch?v={vid}', download=False)
            if info:
                qualities, audio_url, headers = yt_pick_formats(info)
                if qualities or audio_url:
                    found = (qualities, audio_url, headers)
                    break
                notes.append(f'{clients or "default"}: no usable formats')
        except Exception as e:
            notes.append(f'{clients or "default"}: ' + re.sub(r'\s+', ' ', str(e))[:110])

    if not found:
        joined = ' | '.join(notes)
        if re.search(r'sign in|not a bot|confirm you', joined, re.I):
            msg = ('YouTube is asking this server to prove it is not a bot, so the video cannot be fetched '
                   'right now. The Thumbnail tab still works.')
        elif re.search(r'private|unavailable|removed|members', joined, re.I):
            msg = 'This video is private, removed, or not available to download.'
        else:
            msg = 'Could not get this YouTube video. It may be restricted, or YouTube may be blocking the request.'
        return {'error': msg + ' [DEBUG: ' + joined[:380] + ']'}

    qualities, audio_url, headers = found
    best = qualities[0]['url'] if qualities else None
    return {
        'video_id': vid,
        'title': (title or info.get('title') or '')[:150],
        'duration': info.get('duration'),
        'thumbnail': (thumbs[0]['url'] if thumbs else info.get('thumbnail') or ''),
        'thumbnails': thumbs,
        'qualities': qualities,
        'audio_url': audio_url,
        'h': yt_token(headers),
        'download_urls': [best] if best else [],
        'is_image': False,
    }


# ---------------------------------------------------------------------------
# Cobalt gateway (optional). Set the COBALT_API_URL environment variable in Vercel to the address of
# a Cobalt instance (several can be given, separated by commas; COBALT_API_KEY is optional).
# When it is set, YouTube video / MP3 downloads are requested from Cobalt instead of yt-dlp.
# ---------------------------------------------------------------------------
COBALT_QUALITIES = ('144', '240', '360', '480', '720', '1080', '1440', '2160')


def cobalt_instances():
    return [u.strip().rstrip('/') for u in os.environ.get('COBALT_API_URL', '').split(',') if u.strip()]


def cobalt_get(yt_url, kind, quality):
    """Asks Cobalt for one file. Returns ({'url','filename'}, notes)."""
    quality = quality if quality in COBALT_QUALITIES else '720'
    payload = {
        'url': yt_url,
        'downloadMode': 'audio' if kind == 'audio' else 'auto',
        'audioFormat': 'mp3',
        'audioBitrate': '128',
        'videoQuality': quality,
        'youtubeVideoCodec': 'h264',
        'filenameStyle': 'basic',
    }
    headers = {'Accept': 'application/json', 'Content-Type': 'application/json', 'User-Agent': UA}
    key = os.environ.get('COBALT_API_KEY', '').strip()
    if key:
        headers['Authorization'] = 'Api-Key ' + key

    notes = []
    for base in cobalt_instances():
        host = urllib.parse.urlparse(base).hostname or base
        try:
            req = urllib.request.Request(base + '/', data=json.dumps(payload).encode('utf-8'),
                                         headers=headers, method='POST')
            try:
                with urllib.request.urlopen(req, timeout=28) as r:
                    body = json.loads(r.read().decode('utf-8', errors='ignore'))
            except urllib.error.HTTPError as e:
                try:
                    body = json.loads(e.read().decode('utf-8', errors='ignore'))
                except Exception:
                    notes.append(f'{host}: http {e.code}')
                    continue
            status = body.get('status')
            if status in ('tunnel', 'redirect', 'stream') and body.get('url'):
                return {'url': body['url'], 'filename': body.get('filename') or ''}, notes
            if status == 'picker':
                for it in body.get('picker') or []:
                    if it.get('url'):
                        return {'url': it['url'], 'filename': ''}, notes
                if body.get('audio'):
                    return {'url': body['audio'], 'filename': ''}, notes
            err = body.get('error')
            code = err.get('code') if isinstance(err, dict) else err
            notes.append(f'{host}: {code or status or "no result"}')
        except Exception as e:
            notes.append(f'{host}: ' + re.sub(r'\s+', ' ', str(e))[:80])
    return None, notes


def ffmpeg_path():
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return shutil.which('ffmpeg')


def fetch_all(target_url, headers, max_in):
    """Whole file as bytes. YouTube's servers slow down plain downloads, so those are fetched in
    10 MB pieces with &range=, the way yt-dlp does it."""
    host = (urllib.parse.urlparse(target_url).hostname or '').lower()
    if not host.endswith('googlevideo.com'):
        with urllib.request.urlopen(urllib.request.Request(target_url, headers=headers), timeout=25) as r:
            return r.read(max_in + 1)
    data, start, step = b'', 0, 10 * 1024 * 1024
    while len(data) <= max_in:
        u = f'{target_url}&range={start}-{start + step - 1}'
        with urllib.request.urlopen(urllib.request.Request(u, headers=headers), timeout=25) as r:
            part = r.read()
        data += part
        if len(part) < step:
            break
        start += step
    return data


def convert_to_mp3(req_headers, target_url, max_in=40_000_000, max_out=4_200_000, bitrate=128):
    """Downloads an audio file and re-encodes it to a real MP3. Returns bytes, or None when ffmpeg
    is missing / fails / the result would be too big for a Vercel response (caller falls back)."""
    exe = ffmpeg_path()
    if not exe:
        return None
    try:
        h = {k: v for k, v in req_headers.items() if k.lower() != 'range'}
        data = fetch_all(target_url, h, max_in)
        if len(data) > max_in:
            return None
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, 'in.bin')
            with open(src, 'wb') as f:
                f.write(data)
            out = subprocess.run(
                [exe, '-v', 'error', '-i', src, '-vn', '-codec:a', 'libmp3lame', '-b:a', f'{bitrate}k', '-f', 'mp3', 'pipe:1'],
                capture_output=True, timeout=40)
        if out.returncode != 0 or not out.stdout or len(out.stdout) > max_out:
            return None
        return out.stdout
    except Exception:
        return None


class handler(BaseHTTPRequestHandler):
    def _json(self, code, payload):
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(json.dumps(payload).encode('utf-8'))

    def do_GET(self):
        if '?url=' in self.path:
            target_url = None
            req_headers = {}
            try:
                parsed_url = urllib.parse.urlparse(self.path)
                query_params = urllib.parse.parse_qs(parsed_url.query)
                target_url = query_params.get('url', [None])[0]
                filename = query_params.get('filename', ['media-download'])[0].replace('"', '')
                inline = query_params.get('inline', ['0'])[0] == '1'  # 1 = play in the page, 0 = download
                audio = query_params.get('audio', ['0'])[0] == '1'    # 1 = audio file: fix its type and extension

                if not target_url or not host_allowed(target_url):
                    return self._json(400, {'error': 'Invalid or disallowed URL'})

                req_headers = proxy_request_headers(
                    target_url, query_params.get('h', [None])[0], self.headers.get('Range'))

                # Facebook audio: convert to a real MP3 when ffmpeg is available (otherwise falls through to M4A)
                if query_params.get('mp3', ['0'])[0] == '1':
                    try:
                        br = int(query_params.get('br', ['128'])[0])
                    except ValueError:
                        br = 128
                    mp3 = convert_to_mp3(req_headers, target_url, bitrate=br if 32 <= br <= 128 else 128)
                    if mp3:
                        name = re.sub(r'\.[A-Za-z0-9]+$', '', filename) + '.mp3'
                        self.send_response(200)
                        self.send_header('Content-Type', 'audio/mpeg')
                        self.send_header('Content-Disposition', f'attachment; filename="{name}"')
                        self.send_header('Content-Length', str(len(mp3)))
                        self.send_header('Access-Control-Allow-Origin', '*')
                        self.end_headers()
                        self.wfile.write(mp3)
                        return

                req = urllib.request.Request(target_url, headers=req_headers)
                with urllib.request.urlopen(req, timeout=25) as remote_file:
                    content_type = remote_file.headers.get('Content-Type', 'application/octet-stream')

                    # TikTok's audio is usually AAC inside an MP4 container, not a real MP3. Look at the
                    # first bytes and label it correctly, so phones list it under Music, not Videos.
                    first = b''
                    if audio and remote_file.status == 200:
                        first = remote_file.read(8192)
                        kind = sniff_audio(first)
                        if kind:
                            content_type = 'audio/mp4' if kind == 'm4a' else 'audio/mpeg'
                            filename = re.sub(r'\.[A-Za-z0-9]+$', '', filename) + '.' + kind

                    self.send_response(remote_file.status)
                    self.send_header('Content-Type', content_type)
                    self.send_header('Content-Disposition',
                                     f'{"inline" if inline else "attachment"}; filename="{filename}"')
                    self.send_header('Access-Control-Allow-Origin', '*')
                    self.send_header('Accept-Ranges', remote_file.headers.get('Accept-Ranges', 'bytes'))

                    if remote_file.headers.get('Content-Length'):
                        self.send_header('Content-Length', remote_file.headers.get('Content-Length'))
                    if remote_file.headers.get('Content-Range'):
                        self.send_header('Content-Range', remote_file.headers.get('Content-Range'))

                    self.end_headers()

                    if first:
                        self.wfile.write(first)
                    while True:
                        chunk = remote_file.read(8192)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                return
            except (BrokenPipeError, ConnectionResetError):
                return  # the video player closed the connection (normal while seeking / previewing)
            except urllib.error.HTTPError as e:
                return self._json(502, {
                    'error': f'Upstream {e.code} from {urllib.parse.urlparse(target_url or "").hostname} '
                             f'(cookie sent: {"Cookie" in req_headers})'
                })
            except Exception as e:
                return self._json(500, {'error': str(e)})

        self._json(200, {'status': 'API is live and running'})

    def do_POST(self):
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length).decode('utf-8')
            data = json.loads(body)
            raw_url = data.get('url', '')

            if not raw_url:
                return self._json(400, {'error': 'Invalid or empty URL'})

            # ---- TikTok Route ----
            if 'tiktok.com' in raw_url:
                result = extract_tiktok(raw_url)
                if 'error' in result:
                    return self._json(500, {'error': result['error']})
                return self._json(200, result)

            # ---- Facebook Route ----
            if is_facebook_url(raw_url):
                result = extract_facebook(raw_url)
                if 'error' in result:
                    return self._json(500, {'error': result['error']})
                return self._json(200, result)

            # ---- YouTube Route ----
            if 'youtube.com' in raw_url or 'youtu.be' in raw_url:
                try:
                    gateway_payload = json.dumps({
                        "url": raw_url,
                        "vQuality": "max"
                    }).encode('utf-8')

                    req = urllib.request.Request(
                        "https://api.cobalt.tools/api/json",
                        data=gateway_payload,
                        headers={
                            'User-Agent': UA,
                            'Accept': 'application/json',
                            'Content-Type': 'application/json'
                        },
                        method='POST'
                    )

                    with urllib.request.urlopen(req, timeout=25) as resp:
                        res_data = json.loads(resp.read().decode('utf-8', errors='ignore'))
                        status = res_data.get('status')

                        if status in ['stream', 'redirect']:
                            media_url = res_data.get('url')
                            filename_title = res_data.get('filename', 'youtube-video.mp4')
                            if media_url:
                                return self._json(200, {
                                    'download_urls': [media_url],
                                    'is_image': False,
                                    'title': filename_title
                                })
                        
                        return self._json(500, {'error': 'Could not extract YouTube media stream.'})
                except Exception as yt_ex:
                    return self._json(500, {'error': 'YouTube extraction error: ' + str(yt_ex)[:200]})

            # ---- Instagram Route ----
            if 'instagram.com' not in raw_url:
                return self._json(400, {'error': 'Invalid social media URL'})

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
            for attempt in range(2 if is_reel else 1):
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

            # Step 2: fallback for photo-only posts
            if not media_urls and post_code and not is_reel:
                media_urls, diag = extract_images_via_ytdlp_dump(clean_url, post_code)
                is_image = True

            # Step 3: last resort embed scrape
            if not media_urls and post_code and not is_reel:
                found, diag2 = extract_images_fallback(post_code)
                media_urls = found
                diag = (diag + ' | ' if diag else '') + diag2
                is_image = True

            if media_urls:
                cleaned_urls = list(dict.fromkeys(media_urls))
                info_text = diag
                if is_image:
                    info_text += f' | probe_first={probe(cleaned_urls[0])}'
                return self._json(200, {'download_urls': cleaned_urls, 'is_image': is_image, 'info': info_text})

            details = ''
            if last_error:
                details += 'yt-dlp: ' + last_error[:250]
            if diag:
                details += (' || ' if details else '') + diag[:400]

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
