"""
Universal extractor plugin for yt-dlp.

Two entry points:

* ``_UniversalGenericIE`` replaces yt-dlp's built-in generic extractor. The
  built-in checks run first and unchanged; when they find nothing (or when a
  sweep is forced) a pipeline of extra static checks looks for media hidden in
  JSON state, player configs, data attributes, link/meta tags, encoded strings,
  linked scripts and iframes.
* ``UniversalIE`` matches ``universal:<url>`` and forces generic + full sweep on
  any URL, including ones a site-specific extractor would otherwise claim.
* ``UniversalSiteIE`` claims every URL of the sites listed in
  ``~/.config/yt-dlp/universal-sites.json`` (written by ``dlp-add-site``) and
  runs the sweep with the options stored for that site.

Every check yields ``_Candidate`` objects. Candidates are identified by content
(not by file extension), turned into formats with yt-dlp's own manifest
parsers, grouped into videos and merged.

When the sweep finds nothing, two last resorts run before giving up:

* ``borrow`` runs the logic of site-specific extractors whose URL pattern fits
  this page's path (best of all, whose site the page mentions), as if the page
  were on their site: their requests to that site are sent to this one.
* ``browser`` opens the page in a headless browser (Playwright), lets the player
  start, and resolves the media requests it makes.

Options: ``--extractor-args "generic:KEY[=VALUE];..."`` (``universal:`` works as
an alias): sweep, report, merge=smart|all|none, checks=, skip_checks=,
max_scripts=, max_candidates=, iframe_depth=, probe_siblings, borrow_ies=,
borrow_max=, borrow_time=, borrow_min_path=, browser_wait=, browser_channel=.
"""

import base64
import binascii
import contextlib
import dataclasses
import ipaddress
import itertools
import json
import os
import posixpath
import re
import time
import traceback
import urllib.parse

from yt_dlp.extractor.common import InfoExtractor
from yt_dlp.extractor.generic import GenericIE
from yt_dlp.networking import Request
from yt_dlp.utils import (
    NO_DEFAULT,
    ExtractorError,
    UnsupportedError,
    determine_protocol,
    extract_attributes,
    float_or_none,
    int_or_none,
    is_html,
    js_to_json,
    mimetype2ext,
    parse_duration,
    parse_resolution,
    render_table,
    smuggle_url,
    traverse_obj,
    unescapeHTML,
    unsmuggle_url,
    variadic,
)

# --------------------------------------------------------------------------
# URL recognition
# --------------------------------------------------------------------------

_MANIFEST_KINDS = {'m3u8': 'hls', 'mpd': 'dash', 'f4m': 'hds', 'smil': 'smil'}
_VIDEO_EXTS = ('mp4', 'm4v', 'webm', 'mov', 'mkv', 'flv', 'ogv', '3gp')
_AUDIO_EXTS = ('m4a', 'mp3', 'aac', 'ogg', 'oga', 'opus', 'wav', 'flac', 'weba')
_MEDIA_EXTS = (*_MANIFEST_KINDS, *_VIDEO_EXTS, *_AUDIO_EXTS)
_EXT_ALT = '|'.join(sorted(_MEDIA_EXTS, key=len, reverse=True))
# ".mp4" not followed by more name ("a.mp4.jpg", "a.mp4-thumb"), or an ISM manifest
_EXT_TAIL = rf'(?:\.(?:{_EXT_ALT})(?![\w-])(?!\.\w)|\.isml?/manifest)'
_UC = r'''[^\s"'<>\\^`{|}]'''  # characters allowed inside a URL we pick out of text

_ABS_URL_RE = re.compile(rf'(?i)(?:https?:)?//{_UC}+?{_EXT_TAIL}(?:[?#/(]{_UC}*)?')
_QUOTED_URL_RE = re.compile(rf'''(?i)(["'])({_UC}+?{_EXT_TAIL}(?:[?#/(]{_UC}*)?)\1''')
_HINT_RE = re.compile(rf'(?i)\.(?:{_EXT_ALT})\b|\.isml?/')
_PCT_URL_RE = re.compile(r'''(?i)https?%(?:25)?3A%(?:25)?2F%(?:25)?2F[^\s"'<>\\&]+''')
_B64_RE = re.compile(
    r'''(?<![A-Za-z0-9+/_=-])(?:aHR0c[A-Za-z0-9+/_-]{12,}|(?<=["'])[A-Za-z0-9+/_-]{40,})={0,2}''')

_NON_MEDIA_EXT_RE = re.compile(
    r'(?i)\.(?:jpe?g|png|gif|webp|avif|svg|ico|bmp|html?|php|aspx?|jsp|js|mjs|css|json|xml|vtt|srt'
    r'|ttml|ass|txt|pdf|woff2?|ttf|eot|map|zip|m4s|ts)(?:$|[?#])')
_JUNK_HOST_RE = re.compile(
    r'(?i)(?:^|\.)(?:doubleclick\.net|googlesyndication\.com|google-analytics\.com|googletagmanager\.com'
    r'|googletagservices\.com|imasdk\.googleapis\.com|adservice\.google\.[a-z.]+|scorecardresearch\.com'
    r'|moatads\.com|amazon-adsystem\.com|facebook\.net|hotjar\.com|cloudflareinsights\.com'
    r'|sentry\.io|newrelic\.com|nr-data\.net|recaptcha\.net|gstatic\.com|disqus\.com)$')
_WIDGET_IFRAME_RE = re.compile(
    r'(?i)//(?:www\.)?(?:facebook\.com/plugins|platform\.twitter\.com|google\.com/recaptcha'
    r'|accounts\.google\.com|syndication\.twitter\.com|disqus\.com)')
_LIB_SCRIPT_RE = re.compile(
    r'(?i)(?:^|[/._-])(?:jquery|react(?:-dom)?|vue|angular|bootstrap|lodash|underscore|moment|polyfill'
    r'|modernizr|swiper|gsap|popper|analytics|gtm|gtag|fbevents|hls(?:\.light)?|dash\.all|shaka-player'
    r'|video(?:js)?|plyr|flowplayer|jwplayer|clappr|recaptcha|sentry|webfont|fontawesome'
    r'|cookie(?:consent|bot)?)(?:[._-](?:min|slim|umd|esm|prod|production|development|bundle|\d[\w.]*))*\.m?js(?:$|\?)')
_GOOD_SCRIPT_RE = re.compile(r'(?i)player|video|media|stream|embed|playlist|config|episode|watch')

_SPECIFIC_KEY_RE = re.compile(
    r'(?i)hls|m3u8|dash(?!board)|mpd|manifest|playback|stream|smooth|mp4|webm|progressive'
    r'|video_?(?:url|src|file|path)|media_?(?:url|src|file)|content_?url|file_?url|source_?url|download_?url')
_GENERIC_KEYS = frozenset(('src', 'source', 'url', 'file', 'uri', 'href', 'path', 'link'))
_MIME_KEYS = ('type', 'mimeType', 'mime_type', 'mime', 'contentType', 'content_type', 'format', 'encodingFormat')
_PLAYLIST_KEYS = frozenset((
    'playlist', 'items', 'videos', 'entries', 'episodes', 'clips', 'results', 'list', 'medias',
    'related', 'recommendations', 'edges', 'nodes', 'children'))
_META_KEYS = frozenset((
    'title', 'headline', 'duration', 'poster', 'thumbnail', 'thumbnailurl', 'thumbnail_url',
    'videoid', 'video_id', 'mediaid', 'media_id', 'sources'))
_ATTR_HINT_RE = re.compile(
    r'(?i)\bdata-[\w-]*(?:src|video|hls|dash|stream|file|url|m3u8|mpd|source|manifest|setup|config'
    r'|options|player|media|mp4|playback)[\w-]*\s*=')
_SPECIFIC_ATTR_RE = re.compile(r'(?i)hls|dash|m3u8|mpd|stream|manifest|playback|video-?(?:src|url|file)')

_CONFIG_START_RE = re.compile(r'''(?x)
    (?:
        \b(?:sources?|playlist|file|video|media|player(?:Config|Options|Setup|Data)?|config|options|settings)
            ["']?\s*[:=]\s*
        | \.(?:setup|init|load|src|source|configure|create)\s*\(\s*
        | \bnew\s+[\w$.]*(?:Player|Plyr|Playerjs|Hls|DPlayer|Artplayer)\s*\(\s*(?:[^(){}\[\]]{0,80},\s*)?
        | \b(?:videojs|jwplayer|flowplayer|Playerjs)\s*\(\s*(?:[^(){}\[\]]{0,80},\s*)?
    )(?=[{\[])''')
_CONFIG_PAIR_RE = re.compile(
    r'''(?i)["']?([\w$]*(?:file|src|source|url|hls|dash|m3u8|mpd|manifest|stream|mp4)[\w$]*)["']?\s*:\s*'''
    r'''(["'])((?:(?!\2)[^\s\\])+)\2''')
_CALL_RES = tuple((re.compile(pattern), kind, needs_ext) for pattern, kind, needs_ext in (
    (r'''\.loadSource\(\s*(["'])(?P<u>(?:(?!\1)[^\s\\])+)\1''', 'hls', False),
    (r'''\.attachSource\(\s*(["'])(?P<u>(?:(?!\1)[^\s\\])+)\1''', 'dash', False),
    (r'''\.initialize\(\s*[^,()]*,\s*(["'])(?P<u>(?:(?!\1)[^\s\\])+)\1''', 'dash', False),
    (r'''\bplayer\.(?:load|src)\(\s*(["'])(?P<u>(?:(?!\1)[^\s\\])+)\1''', None, False),
    (r'''\.src\s*=\s*(["'])(?P<u>(?:(?!\1)[^\s\\])+)\1''', None, True),
))
_SCRIPT_RE = re.compile(r'(?is)<script\b(?P<attrs>[^>]*)>(?P<body>.*?)</script>')

_RESOLUTIONS = '144|240|270|288|360|480|540|576|720|1080|1440|2160|4320'
_QUALITY_TOKEN_RE = re.compile(
    rf'(?i)(?:^|[_.\-])(?:(?:{_RESOLUTIONS})p?|\d{{3,4}}p|\d+k(?:bps)?|[248]k|u?hd|sd|hq|lq|low|med(?:ium)?'
    r'|high|mobile|master|index|playlist|manifest|chunklist|stream|main|default|source|orig(?:inal)?'
    r'|h26[45]|hevc|avc|av1|vp9|aac|mp4|hls|dash|video|audio)(?=$|[_.\-])')


def _url_kind(url):
    """Guess 'hls' / 'dash' / 'ism' / 'hds' / 'smil' / 'http' (progressive) from a URL, else None"""
    path = urllib.parse.urlparse(url).path.lower()
    if re.search(r'\.isml?/manifest', path):
        if 'format=m3u8' in path:
            return 'hls'
        if 'format=mpd' in path:
            return 'dash'
        return 'ism'
    for ext in (path.rpartition('.')[2], *re.findall(rf'\.({_EXT_ALT})(?=/)', path)[-1:]):
        if ext in _MANIFEST_KINDS:
            return _MANIFEST_KINDS[ext]
        if ext in _VIDEO_EXTS or ext in _AUDIO_EXTS:
            return 'http'
    return None


def _mime_kind(mime):
    mime = (mime or '').lower()
    if 'mpegurl' in mime or mime in ('hls', 'm3u8'):
        return 'hls'
    if 'dash' in mime or mime == 'mpd':
        return 'dash'
    if 'vnd.ms-sstr' in mime:
        return 'ism'
    if mime.startswith(('video/', 'audio/')) or mime in ('mp4', 'webm', 'mp3', 'video', 'audio'):
        return 'http'
    return None


def _key_kind(key):
    key = key.lower()
    if 'hls' in key or 'm3u8' in key:
        return 'hls'
    if 'dash' in key or 'mpd' in key:
        return 'dash'
    if 'smooth' in key:
        return 'ism'
    return None


def _js_unescape(text):
    """Undo the JS/JSON string escapes that hide URLs: \\/  \\u002F  \\x2F"""
    if '\\' not in text:
        return text
    text = re.sub(r'\\u([0-9a-fA-F]{4})', lambda m: chr(int(m.group(1), 16)), text)
    text = re.sub(r'\\x([0-9a-fA-F]{2})', lambda m: chr(int(m.group(1), 16)), text)
    for _ in range(3):
        if '\\/' not in text:
            break
        text = text.replace('\\/', '/')
    return text


def _b64_text(blob):
    blob = blob.rstrip('=').replace('-', '+').replace('_', '/')
    try:
        raw = base64.b64decode(blob + '=' * (-len(blob) % 4), validate=True)
        text = raw.decode('utf-8')
    except (binascii.Error, ValueError):
        return None
    return text if text.isprintable() or '\n' in text else None


def _tidy(url):
    url = unescapeHTML(url).strip().rstrip('.,;')
    while url.endswith(')') and url.count(')') > url.count('('):
        url = url[:-1]
    return url


def _scan_text(text, *, relative=True, hidden=True):
    """
    Yield media-looking URL strings found in free text. Looks at the text
    JS-unescaped, at percent-encoded URL tokens and at base64 blobs.
    Relative results are only returned from quoted strings. With hidden=True a
    base64-encoded URL is returned even when it has no media extension.
    """
    def scan(chunk, rel):
        if not _HINT_RE.search(chunk):
            return
        for mobj in _ABS_URL_RE.finditer(chunk):
            yield _tidy(mobj.group(0))
        if not rel:
            return
        for mobj in _QUOTED_URL_RE.finditer(chunk):
            url = _tidy(mobj.group(2))
            if url.startswith(('http://', 'https://', '//')):
                continue  # already found by the absolute pattern
            # bare file names in scripts are too noisy unless they are manifests
            if '/' in url or _url_kind(url) not in (None, 'http'):
                yield url

    text = _js_unescape(text)
    yield from scan(text, relative)

    for mobj in _PCT_URL_RE.finditer(text):
        decoded = urllib.parse.unquote(urllib.parse.unquote(mobj.group(0)))
        yield from scan(decoded, False)

    for n, mobj in enumerate(_B64_RE.finditer(text)):
        if n >= 80:
            break
        blob = mobj.group(0)
        decoded = _b64_text(blob) if len(blob) <= 20000 else None
        if not decoded:
            continue
        decoded = _js_unescape(decoded)
        found = list(scan(decoded, True))
        yield from found
        if hidden and not found and blob.startswith('aHR0c') and re.fullmatch(r'https?://\S+', decoded):
            # a base64-hidden URL with no extension: worth sniffing
            if not _NON_MEDIA_EXT_RE.search(decoded):
                yield decoded


def _balanced(text, start, limit=200_000):
    """Return the bracketed JS literal starting at text[start], or None"""
    pairs = {'{': '}', '[': ']', '(': ')'}
    stack, quote, i, end = [], None, start, min(len(text), start + limit)
    while i < end:
        ch = text[i]
        if quote:
            if ch == '\\':
                i += 2
                continue
            if ch == quote:
                quote = None
        elif ch in '"\'`':
            quote = ch
        elif ch in pairs:
            stack.append(pairs[ch])
        elif stack and ch == stack[-1]:
            stack.pop()
            if not stack:
                return text[start:i + 1]
        elif ch in '}])':
            return None
        i += 1
    return None


def _sniff(first, content_type):
    """Identify media by its first bytes. Returns (kind, ext) or (None, reason)"""
    head = first.lstrip(b'\xef\xbb\xbf \t\r\n')
    if head.startswith(b'#EXTM3U'):
        return 'hls', 'm3u8'
    if head.startswith(b'<'):
        low = head[:2048].lower()
        if b'<mpd' in low:
            return 'dash', 'mpd'
        if b'<smoothstreamingmedia' in low:
            return 'ism', 'ism'
        if b'ns.adobe.com/f4m' in low:
            return 'hds', 'f4m'
        if b'<smil' in low:
            return 'smil', 'smil'
        return None, 'html page' if is_html(first) else 'not media (xml)'
    box = first[4:8]
    if box == b'ftyp':
        brand = first[8:12]
        if brand.startswith((b'M4A', b'M4B')):
            return 'http', 'm4a'
        if brand.startswith(b'qt'):
            return 'http', 'mov'
        if brand.startswith(b'3g'):
            return 'http', '3gp'
        return 'http', 'mp4'
    if box in (b'styp', b'moof', b'sidx'):
        return None, 'media segment'
    if first.startswith(b'\x1a\x45\xdf\xa3'):
        return 'http', 'webm' if b'webm' in first[:64] else 'mkv'
    if first.startswith(b'OggS'):
        return 'http', 'ogg'
    if first.startswith(b'ID3'):
        return 'http', 'mp3'
    if first.startswith(b'fLaC'):
        return 'http', 'flac'
    if first.startswith(b'RIFF') and first[8:12] == b'WAVE':
        return 'http', 'wav'
    if first.startswith(b'FLV'):
        return 'http', 'flv'
    if len(first) > 188 and first[0] == 0x47 and first[188] == 0x47:
        return None, 'media segment'
    if len(first) > 1 and first[0] == 0xFF and first[1] & 0xE0 == 0xE0:
        return 'http', 'aac' if first[1] & 0xF6 == 0xF0 else 'mp3'

    ctype = content_type.lower().partition(';')[0].strip()
    kind = _mime_kind(ctype)
    if kind == 'http':
        return 'http', mimetype2ext(ctype, default=None)
    if kind:
        return kind, None
    return None, f'not media ({ctype or "unknown type"})'


@dataclasses.dataclass
class _Stem:
    """What a media URL says about which video it belongs to"""
    host: str
    dirname: str
    base: str  # file name without extension and quality markers
    tokens: frozenset  # id-looking path parts

    @classmethod
    def of(cls, url):
        if not url:
            return None
        parsed = urllib.parse.urlparse(url)
        path = re.sub(r'(?i)(\.isml?)/.*$', r'\1', parsed.path)
        dirname, name = posixpath.split(path)
        base = name.rpartition('.')[0] or name
        for _ in range(4):
            stripped = _QUALITY_TOKEN_RE.sub('', base)
            if stripped == base:
                break
            base = stripped
        base = base.strip('_.-').lower()
        parts = [*dirname.lower().split('/'), base]
        tokens = frozenset(p for p in parts if len(p) >= 5 and re.search(r'\d', p))
        return cls(parsed.netloc.lower(), dirname, base, tokens)

    def related(self, other):
        if self.tokens & other.tokens:
            return True
        if self.host != other.host:
            return False
        if self.dirname == other.dirname:
            return True
        short, long_ = sorted((self.dirname, other.dirname), key=len)
        return short.count('/') >= 2 and long_.startswith(short + '/')


@dataclasses.dataclass
class _Candidate:
    """A URL some check thinks might be media"""
    url: str
    source: str
    kind: str = None  # hint only; the resolver decides
    group: str = None  # candidates sharing a group came from one player/object
    referer: str = None
    hints: dict = dataclasses.field(default_factory=dict)  # per format: height, width, tbr, label
    meta: dict = dataclasses.field(default_factory=dict)  # per video: title, duration, thumbnail
    headers: dict = dataclasses.field(default_factory=dict)  # extra request headers (browser check)


@dataclasses.dataclass
class _Item:
    """Formats that came from one manifest, one file or one built-in entry"""
    kind: str
    url: str
    formats: list
    subtitles: dict
    source: str
    group: str = None
    info: dict = dataclasses.field(default_factory=dict)
    base: dict = None  # a complete built-in entry to keep as the starting point

    def __post_init__(self):
        self.stem = _Stem.of(self.url)


@dataclasses.dataclass
class _Page:
    url: str
    webpage: str
    urlh: object
    video_id: str
    title: str
    depth: int = 0
    prefix: str = ''  # namespaces group keys per page

    def scripts(self):
        if not hasattr(self, '_scripts'):
            self._scripts = [
                (m.group('attrs'), m.group('body'), m.start('body'))
                for m in _SCRIPT_RE.finditer(self.webpage) if m.group('body').strip()]
        return self._scripts


class _Run:
    """State of one extraction"""

    def __init__(self, prefixed, site=None):
        self.forced = prefixed
        self.prefixed = prefixed
        self.site = site
        args = (_SITES.get(site) or {}).get('args') if site else None
        self.site_args = args if isinstance(args, dict) else {}
        self.busy = False
        self.swept = False
        self.redirect_to = None
        self.builtin = None
        self.stats = {}  # check name -> {'found', 'time', 'error'}
        self.rows = []  # one per candidate, for the report
        self.counters = {}
        self.borrow_tried = False
        self.browser_tried = False
        self.borrowed = None  # key of the extractor whose logic worked

    def stat(self, name):
        return self.stats.setdefault(name, {'found': 0, 'time': 0.0, 'error': None, 'formats': 0, 'media': 0})

    def next_id(self, prefix):
        n = self.counters[prefix] = self.counters.get(prefix, 0) + 1
        return prefix if n == 1 else f'{prefix}{n}'


# --------------------------------------------------------------------------
# Site profiles
# --------------------------------------------------------------------------

def _sites_file():
    explicit = os.environ.get('YTDLP_UNIVERSAL_SITES')
    if explicit:
        return explicit
    config = os.environ.get('XDG_CONFIG_HOME') or os.path.join(os.path.expanduser('~'), '.config')
    return os.path.join(config, 'yt-dlp', 'universal-sites.json')


def _load_sites():
    """{domain: profile} from the sites file; a profile is {'args': {key: [values]}, ...}"""
    try:
        with open(_sites_file(), encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    sites = data.get('sites') if isinstance(data, dict) else None
    if not isinstance(sites, dict):
        return {}
    return {
        str(domain).lower(): profile for domain, profile in sites.items()
        if isinstance(profile, dict) and profile.get('enabled', True)
        and re.fullmatch(r'[\w.-]+', str(domain))}


_SITES = _load_sites()


def _site_for(url):
    """The most specific configured domain this URL belongs to"""
    host = (urllib.parse.urlparse(url).hostname or '').lower()
    matches = [d for d in _SITES if host == d or host.endswith('.' + d)]
    return max(matches, key=len, default=None)


# --------------------------------------------------------------------------
# Borrowing site extractors
# --------------------------------------------------------------------------

_BORROW_SKIP = frozenset(('generic', 'universal', 'universalsite'))
_TEXT_URL_RE = re.compile(r'''(?i)(?:https?:)?//[^\s"'<>\\]+''')


def _base_host(host):
    """example.com for www.example.com or m.example.com; anything else unchanged"""
    host = (host or '').lower().rstrip('.')
    for prefix in ('www.', 'm.'):
        if host.startswith(prefix) and host.count('.') >= 2:
            return host[len(prefix):]
    return host


def _is_ip(host):
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


_IE_HOSTS = {}


def _ie_hosts(cls):
    """(scheme, host) of the extractor's own site, taken from its test URLs"""
    if cls not in _IE_HOSTS:
        hosts = []
        tests = [getattr(cls, '_TEST', None), *(getattr(cls, '_TESTS', None) or [])]
        for test in tests:
            url = test.get('url') if isinstance(test, dict) else None
            if not isinstance(url, str):
                continue
            try:
                parsed = urllib.parse.urlparse(url)
            except ValueError:
                continue
            pair = (parsed.scheme if parsed.scheme in ('http', 'https') else 'https', (parsed.hostname or '').lower())
            if pair[1] and pair not in hosts:
                hosts.append(pair)
        _IE_HOSTS[cls] = hosts
    return _IE_HOSTS[cls]


class _HostMap:
    """Sends a borrowed extractor's requests for its own site to the page's site"""

    def __init__(self, orig_host, page_url):
        page = urllib.parse.urlparse(page_url)
        self.orig = _base_host(orig_host)
        self.scheme = page.scheme or 'https'
        self.netloc = page.netloc.rpartition('@')[2]
        self.clone_base = _base_host(page.hostname)
        # api.example.com -> api.clone.net, but not on an IP address or a bare name
        self.subdomains = not _is_ip(page.hostname or '') and '.' in self.clone_base

    def netloc_for(self, host):
        """(new netloc, new scheme or None) for a host of the original site, else (None, None)"""
        host = (host or '').lower().rstrip('.')
        if host in (self.orig, f'www.{self.orig}', f'm.{self.orig}'):
            return self.netloc, self.scheme
        if self.subdomains and host.endswith('.' + self.orig):
            return host[:-len(self.orig)] + self.clone_base, None
        return None, None

    def url(self, url):
        if not isinstance(url, str) or '//' not in url:
            return url
        try:
            parsed = urllib.parse.urlparse(url)
            netloc, scheme = self.netloc_for(parsed.hostname)
        except ValueError:
            return url
        if not netloc:
            return url
        # keep protocol-relative URLs protocol-relative
        new_scheme = (scheme or parsed.scheme) if parsed.scheme else ''
        return parsed._replace(netloc=netloc, scheme=new_scheme).geturl()

    def text(self, text):
        if not isinstance(text, str) or self.orig not in text.lower():
            return text
        return _TEXT_URL_RE.sub(lambda m: self.url(m.group(0)), text)

    def deep(self, obj):
        if isinstance(obj, str):
            return self.text(obj)
        if isinstance(obj, dict):
            return {k: self.deep(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return type(obj)(self.deep(v) for v in obj)
        return obj


def _borrowed_class(cls, hostmap):
    """A subclass of a site extractor that works on another site and never logs in"""

    def _request_webpage(self, url_or_request, *args, **kwargs):
        if isinstance(url_or_request, Request):
            url_or_request.url = hostmap.url(url_or_request.url)
            for key, value in list(url_or_request.headers.items()):
                url_or_request.headers[key] = hostmap.text(value)
        else:
            url_or_request = hostmap.url(url_or_request)
        if kwargs.get('headers'):
            kwargs['headers'] = {k: hostmap.text(v) for k, v in kwargs['headers'].items()}
        return super(borrowed, self)._request_webpage(url_or_request, *args, **kwargs)

    def _set_cookie(self, domain, *args, **kwargs):
        netloc, _ = hostmap.netloc_for(domain.lstrip('.'))
        if netloc:
            host = urllib.parse.urlparse(f'//{netloc}').hostname
            super(borrowed, self)._set_cookie(('.' if domain.startswith('.') else '') + host, *args, **kwargs)
        return super(borrowed, self)._set_cookie(domain, *args, **kwargs)

    def _get_cookies(self, url):
        return super(borrowed, self)._get_cookies(hostmap.url(url))

    borrowed = type(cls.__name__, (cls,), {
        '__module__': cls.__module__,
        '__qualname__': cls.__qualname__,
        # credentials for the original site must never be sent to another one
        '_NETRC_MACHINE': None,
        '_perform_login': lambda self, username, password: None,
        '_request_webpage': _request_webpage,
        '_set_cookie': _set_cookie,
        '_get_cookies': _get_cookies,
    })
    return borrowed


# --------------------------------------------------------------------------
# Extractors
# --------------------------------------------------------------------------

class UniversalIE(InfoExtractor):
    IE_NAME = 'universal'
    IE_DESC = 'Force the generic extractor plus the universal sweep: universal:URL'
    _VALID_URL = r'universal:(?P<url>.+)'

    def _real_extract(self, url):
        target = self._match_valid_url(url).group('url')
        if target.startswith('//'):
            target = 'https:' + target
        elif not re.match(r'[a-zA-Z][\w+.-]*://', target):
            target = 'https://' + target
        return self.url_result(
            smuggle_url(target, {'to_generic': True, 'universal_sweep': True}), ie='Generic')


class UniversalSiteIE(InfoExtractor):
    IE_NAME = 'universal:site'
    IE_DESC = 'Sites added with dlp-add-site (universal sweep with per-site options)'
    _VALID_URL = (
        r'https?://(?:[^/?#@]+@)?(?:[\w-]+\.)*(?:%s)(?::\d+)?(?:[/?#]|$)'
        % '|'.join(map(re.escape, sorted(_SITES, key=len, reverse=True)))) if _SITES else False

    def _real_extract(self, url):
        return self.url_result(smuggle_url(url, {
            'to_generic': True, 'universal_sweep': True, 'universal_site': _site_for(url),
        }), ie='Generic')


class _UniversalGenericIE(GenericIE, plugin_name='universal'):
    _u_run = None

    # Structured checks come first so their grouping/metadata wins when the
    # same URL is found again by the plain text scan.
    _U_CHECKS = (
        ('json-state', '_u_check_json_state'),
        ('player-config', '_u_check_player_config'),
        ('data-attrs', '_u_check_data_attrs'),
        ('link-meta', '_u_check_link_meta'),
        ('url-scan', '_u_check_url_scan'),
        ('linked-resources', '_u_check_linked_resources'),
        ('iframes', '_u_check_iframes'),
    )

    # ---- options ---------------------------------------------------------

    def _u_arg(self, key, default=None):
        for namespace in ('generic', 'universal'):
            val = traverse_obj(self._downloader.params, ('extractor_args', namespace, key))
            if val is not None:
                return [str(v).lower() for v in val]
        # then whatever dlp-add-site stored for this site
        run = self._u_run
        if run and key in run.site_args:
            return [str(v).lower() for v in variadic(run.site_args[key])]
        return default

    def _configuration_arg(self, key, default=NO_DEFAULT, *, ie_key=None, casesense=False):
        # lets a site profile set the stock generic options too (impersonate, ...)
        run = self._u_run
        if (run and ie_key is None and key in run.site_args
                and traverse_obj(self._downloader.params, ('extractor_args', 'generic', key)) is None):
            values = [str(v) for v in variadic(run.site_args[key])]
            return values if casesense else [v.lower() for v in values]
        return super()._configuration_arg(key, default, ie_key=ie_key, casesense=casesense)

    def _u_flag(self, key):
        val = self._u_arg(key)
        return val is not None and 'false' not in val

    def _u_int(self, key, default):
        return int_or_none(traverse_obj(self._u_arg(key), 0), default=default)

    def _u_enabled(self, name):
        only, skip = self._u_arg('checks'), self._u_arg('skip_checks', [])
        return name not in skip and (only is None or name in only)

    def _u_say(self, msg):
        if self.get_param('quiet') and not self.get_param('verbose'):
            self._downloader.to_stderr(f'[{self.IE_NAME}] {msg}')
        else:
            self.to_screen(msg)

    # ---- entry points ----------------------------------------------------

    def report_following_redirect(self, new_url):
        if self._u_run:
            self._u_run.redirect_to = new_url
        super().report_following_redirect(new_url)

    def _real_extract(self, url):
        _, smuggled = unsmuggle_url(url, {})
        prefixed = bool(smuggled.get('universal_sweep'))
        site = smuggled.get('universal_site')
        run = self._u_run = _Run(prefixed, site)
        run.forced = prefixed or self._u_flag('sweep')
        try:
            result = super()._real_extract(url)
        except ExtractorError as e:
            self._u_dump_report(run, url, str(e))
            if isinstance(e, UnsupportedError):
                self._u_print_report(run)
            raise
        finally:
            self._u_run = None
        self._u_print_report(run)
        self._u_dump_report(run, url, None)

        # "universal:" must survive redirects, otherwise the target could be
        # handed to a site-specific extractor after all
        if (prefixed and run.redirect_to and isinstance(result, dict)
                and result.get('_type') == 'url' and not result.get('ie_key')):
            result = {
                **result,
                'url': smuggle_url(result['url'], {
                    'to_generic': True, 'universal_sweep': True, **({'universal_site': site} if site else {})}),
                'ie_key': 'Generic',
            }
        return result

    def _extract_embeds(self, url, webpage, *, urlh=None, info_dict={}):
        run = self._u_run
        if run is None or run.busy:
            # Called by another extractor (or re-entered): stock behaviour
            return super()._extract_embeds(url, webpage, urlh=urlh, info_dict=info_dict)
        run.busy = True
        try:
            return self._u_extract(run, url, webpage, urlh, info_dict)
        finally:
            run.busy = False

    def _u_extract(self, run, url, webpage, urlh, info_dict):
        clean_url, _ = unsmuggle_url(url, {})
        page_url = urlh.url if urlh else clean_url
        video_id = traverse_obj(info_dict, 'display_id', 'id') or self._generic_id(clean_url)

        started, builtin, builtin_error = time.monotonic(), [], None
        try:
            builtin = list(super()._extract_embeds(url, webpage, urlh=urlh, info_dict=info_dict))
        except ExtractorError as e:
            builtin_error = e
        stat = run.stat('builtin')
        stat.update({
            'found': len(builtin), 'time': time.monotonic() - started,
            'error': builtin_error and str(builtin_error.orig_msg if hasattr(builtin_error, 'orig_msg') else builtin_error),
        })
        run.builtin = len(builtin)
        if builtin and not run.forced:
            return builtin

        run.swept = True
        page = _Page(page_url, webpage, urlh, video_id, info_dict.get('title') or video_id)

        # a site learned with dlp-add-site goes straight to what worked there
        if not builtin and 'borrow_ies' in run.site_args:
            entries = self._u_borrow(run, page)
            if entries:
                return entries
        if not builtin and 'browser_first' in run.site_args:
            entries = self._u_browser(run, page)
            if entries:
                return entries

        self.write_debug('Universal sweep: running extra checks')

        items, passthrough = [], []
        for entry in builtin:
            self._u_adopt(entry, 'builtin', None, items, passthrough)
        candidates = []
        for result in self._u_run_checks(run, page):
            if isinstance(result, _Candidate):
                candidates.append(result)
            else:
                self._u_adopt(result, 'iframes', None, items, passthrough)

        known = set()
        for item in items:
            for fmt in item.formats:
                known.update(filter(None, (fmt.get('url'), fmt.get('manifest_url'))))
        items.extend(self._u_resolve_all(run, candidates, known, page))

        if self._u_flag('probe_siblings'):
            items.extend(self._u_probe_siblings(run, items, page))

        entries = self._u_build_entries(items, video_id, page.title)
        if entries:
            # bare redirects/iframes from the built-in checks are only worth
            # keeping next to real media if a real extractor wants them
            passthrough = [e for e in passthrough if e.get('ie_key') or self._u_claimed(e.get('url'))
                           or e.get('_type') not in ('url', None)]
        entries.extend(passthrough)
        if not entries and not builtin:
            # last resorts, in order of cost: another extractor's logic, then a real browser
            entries = self._u_borrow(run, page) or self._u_browser(run, page)
        if not entries and builtin_error:
            raise builtin_error
        return entries

    def _u_adopt(self, entry, source, group, items, passthrough):
        """File a ready-made info dict under items (has formats) or passthrough"""
        formats = entry.get('formats')
        if not formats:
            passthrough.append(entry)
            return
        kinds = {self._u_protocol_kind(f) for f in formats}
        kind = kinds.pop() if len(kinds) == 1 else 'mixed'
        url = traverse_obj(formats, (0, ('manifest_url', 'url')), get_all=False)
        items.append(_Item(
            kind, url, list(formats), entry.get('subtitles') or {}, source, group,
            info={'duration': entry.get('duration')}, base=entry))

    @staticmethod
    def _u_protocol_kind(fmt):
        protocol = determine_protocol(fmt)
        if protocol.startswith('m3u8'):
            return 'hls'
        if protocol == 'http_dash_segments':
            return 'dash'
        if protocol in ('ism', 'f4m'):
            return 'ism' if protocol == 'ism' else 'hds'
        return 'http'

    def _u_claimed(self, url):
        """Name of a site-specific extractor that wants this URL, if any"""
        if not url:
            return None
        for key, ie in self._downloader._ies.items():
            if key in ('Generic', 'Universal'):
                continue
            try:
                if ie.suitable(url):
                    return key
            except Exception:
                continue
        return None

    # ---- check pipeline --------------------------------------------------

    def _u_run_checks(self, run, page):
        results = []
        for name, method in self._U_CHECKS:
            if not self._u_enabled(name):
                continue
            stat, started, before = run.stat(name), time.monotonic(), len(results)
            try:
                results.extend(getattr(self, method)(run, page))
            except Exception as e:  # a broken heuristic must never sink the extraction
                stat['error'] = f'{type(e).__name__}: {e}'
                self.write_debug(f'Universal check {name} failed:\n{traceback.format_exc()}')
            stat['time'] += time.monotonic() - started
            stat['found'] += sum(1 for r in results[before:] if isinstance(r, dict) or r.source == name)
        return results

    def _u_candidate(self, page, url, source, **kwargs):
        """Normalise a found URL into a candidate, or None if it is unusable"""
        if not isinstance(url, str) or not 4 < len(url) < 4096:
            return None
        full = urllib.parse.urljoin(page.url, url.strip())
        parsed = urllib.parse.urlparse(full)
        if parsed.scheme not in ('http', 'https') or not parsed.netloc:
            return None
        if _JUNK_HOST_RE.search(parsed.hostname or ''):
            return None
        full = urllib.parse.urldefrag(full)[0]
        if full == urllib.parse.urldefrag(page.url)[0]:
            return None
        kwargs.setdefault('kind', _url_kind(full))
        return _Candidate(full, source, referer=page.url, **kwargs)

    def _u_walk(self, page, obj, source, group):
        """Yield candidates for media-looking strings inside a JSON-like structure"""
        anchors, budget = {}, [300]

        def meta_of(anchor):
            if not isinstance(anchor, dict):
                return {}
            lowered = {str(k).lower(): v for k, v in anchor.items()}
            duration = lowered.get('duration')
            thumbnail = next((lowered[k] for k in ('poster', 'thumbnail', 'thumbnailurl', 'thumbnail_url', 'image')
                              if isinstance(lowered.get(k), str)), None)
            return {
                'title': next((lowered[k] for k in ('title', 'headline')
                               if isinstance(lowered.get(k), str) and lowered[k].strip()), None),
                'duration': (float_or_none(duration) if isinstance(duration, (int, float))
                             else parse_duration(duration) if isinstance(duration, str) else None),
                'thumbnail': urllib.parse.urljoin(page.url, thumbnail) if thumbnail else None,
                'description': lowered.get('description') if isinstance(lowered.get('description'), str) else None,
            }

        def hints_of(node):
            lowered = {str(k).lower(): v for k, v in node.items()}
            label = next((str(lowered[k]) for k in ('label', 'quality', 'res', 'resolution', 'name')
                          if isinstance(lowered.get(k), (str, int))), None)
            height = int_or_none(lowered.get('height')) or traverse_obj(
                parse_resolution(label or '', lenient=True), 'height')
            return {
                'height': height,
                'width': int_or_none(lowered.get('width')),
                'tbr': float_or_none(lowered.get('bitrate'), scale=1000) if int_or_none(
                    lowered.get('bitrate'), default=0) > 20000 else float_or_none(lowered.get('bitrate')),
                'label': label,
            }

        def anchor_for(chain):
            # chain: innermost first, each (container, key it was reached under)
            for depth, (container, key) in enumerate(chain[:6]):
                if isinstance(container, dict) and _META_KEYS & {str(k).lower() for k in container}:
                    return container
                if isinstance(container, list) and str(key or '').lower() in _PLAYLIST_KEYS:
                    return chain[depth - 1][0] if depth else container
            if len(chain) > 2 and isinstance(chain[1][0], list) and isinstance(chain[2][0], dict):
                return chain[2][0]  # {sources: [{src}, {src}]} -> the owner of the list
            return chain[0][0]

        def consider(node, key, value, chain):
            if not 4 < len(value) < 4096 or re.search(r'[\s<>"]', value):
                if value[:1] in '{[' and _HINT_RE.search(value):
                    try:
                        yield from visit(json.loads(value), key, chain)
                    except ValueError:
                        pass
                return
            kind = _url_kind(value)
            if kind is None:
                rooted = value.startswith(('http://', 'https://', '/'))
                if not rooted or _NON_MEDIA_EXT_RE.search(value):
                    return
                if _SPECIFIC_KEY_RE.search(str(key)):
                    kind = _key_kind(str(key))
                elif str(key).lower() in _GENERIC_KEYS and isinstance(node, dict):
                    kind = next(filter(None, (
                        _mime_kind(node[m]) for m in _MIME_KEYS if isinstance(node.get(m), str))), None)
                    if not kind:
                        return
                    kind = None if kind == 'http' else kind
                else:
                    return
            anchor = anchor_for(chain)
            index = anchors.setdefault(id(anchor), len(anchors))
            cand = self._u_candidate(
                page, value, source, kind=kind, group=f'{group}:{index}',
                hints=hints_of(node) if isinstance(node, dict) else {}, meta=meta_of(anchor))
            if cand:
                budget[0] -= 1
                yield cand

        def visit(node, key, chain):
            if budget[0] <= 0 or len(chain) > 40:
                return
            chain = [(node, key), *chain]
            if isinstance(node, dict):
                for k, v in node.items():
                    if isinstance(v, str):
                        yield from consider(node, k, v, chain)
                    elif isinstance(v, (dict, list)):
                        yield from visit(v, k, chain)
            elif isinstance(node, list):
                for v in node[:500]:
                    if isinstance(v, str):
                        if _url_kind(v):
                            yield from consider(node, key, v, chain)
                    elif isinstance(v, (dict, list)):
                        yield from visit(v, key, chain)

        yield from visit(obj, None, [])

    # ---- the checks ------------------------------------------------------

    def _u_check_json_state(self, run, page):
        """JSON embedded in the page: framework state, JSON script tags, JSON assigned in scripts"""
        blobs, webpage = [], page.webpage

        if 'self.__next_f' in webpage:
            try:
                blobs.append(('next-flight', self._search_nextjs_v13_data(webpage, page.video_id, fatal=False)))
            except Exception:
                pass
        if '__NUXT_DATA__' in webpage:
            try:
                blobs.append(('nuxt-data', self._search_nuxt_json(webpage, page.video_id, default={})))
            except Exception:
                pass
        if '__NUXT__' in webpage and re.search(r'__NUXT__\s*=\s*\(function', webpage):
            try:
                blobs.append(('nuxt', self._search_nuxt_data(webpage, page.video_id, fatal=False)))
            except Exception:
                pass

        decoder, attempts = json.JSONDecoder(strict=False), 0
        for n, (attrs, body, _) in enumerate(page.scripts()):
            attrs = attrs.lower()
            if 'json' in attrs:
                if '__nuxt_data__' in attrs:
                    continue
                try:
                    blobs.append((f'script{n}', json.loads(body, strict=False)))
                except ValueError:
                    pass
                continue
            if 'src=' in attrs or not _HINT_RE.search(_js_unescape(body)) and 'http' not in body:
                continue
            # object/array literals assigned in plain scripts that happen to be strict JSON
            end = 0
            for mobj in re.finditer(r'[\w$\]]\s*=\s*(?=[{\[])', body):
                if mobj.end() < end or attempts >= 200:
                    continue
                attempts += 1
                try:
                    obj, end = decoder.raw_decode(body, mobj.end())
                except ValueError:
                    continue
                blobs.append((f'script{n}@{mobj.end()}', obj))

        for label, obj in blobs:
            if obj:
                yield from self._u_walk(page, obj, 'json-state', f'{page.prefix}json:{label}')

    def _u_check_player_config(self, run, page):
        """JS player setup: config object literals and loadSource()/initialize()/src calls"""
        for n, (attrs, body, _) in enumerate(page.scripts()):
            if 'json' in attrs.lower() or 'src=' in attrs.lower():
                continue
            end = 0
            for mobj in _CONFIG_START_RE.finditer(body):
                if mobj.end() < end:
                    continue  # inside a literal we already handled
                literal = _balanced(body, mobj.end())
                if not literal or len(literal) < 12:
                    continue
                unescaped = _js_unescape(literal)
                if not (_HINT_RE.search(unescaped) or _SPECIFIC_KEY_RE.search(literal)):
                    continue
                end = mobj.end() + len(literal)
                group = f'{page.prefix}config:{n}@{mobj.end()}'
                try:
                    obj = json.loads(js_to_json(literal), strict=False)
                except Exception:
                    obj = None
                if obj is not None:
                    yield from self._u_walk(page, obj, 'player-config', group)
                    continue
                # not parseable (functions, variables...): take the string pairs
                for key, _, value in _CONFIG_PAIR_RE.findall(unescaped):
                    kind = _url_kind(value)
                    if not kind and (_NON_MEDIA_EXT_RE.search(value) or not _SPECIFIC_KEY_RE.search(key)
                                     or not value.startswith(('http://', 'https://', '/'))):
                        continue
                    cand = self._u_candidate(
                        page, value, 'player-config', kind=kind or _key_kind(key), group=group)
                    if cand:
                        yield cand

            unescaped = _js_unescape(body)
            for regex, kind, needs_ext in _CALL_RES:
                for mobj in regex.finditer(unescaped):
                    value = mobj.group('u')
                    url_kind = _url_kind(value)
                    if not url_kind and (needs_ext or _NON_MEDIA_EXT_RE.search(value)
                                         or not value.startswith(('http://', 'https://', '/'))):
                        continue
                    cand = self._u_candidate(
                        page, value, 'player-config', kind=url_kind or kind,
                        group=f'{page.prefix}call:{n}')
                    if cand:
                        yield cand

    def _u_check_data_attrs(self, run, page):
        """data-* attributes holding a media URL or a JSON player config"""
        for n, mobj in enumerate(re.finditer(r'(?s)<[a-zA-Z][\w:-]*\s[^>]*\bdata-[^>]*>', page.webpage)):
            tag = mobj.group(0)
            if not _ATTR_HINT_RE.search(tag):
                continue
            group = f'{page.prefix}attrs:{n}'
            for name, value in extract_attributes(tag).items():
                if not name.startswith('data-') or not value:
                    continue
                value = value.strip()
                if value[:1] in '{[':
                    try:
                        obj = json.loads(value, strict=False)
                    except ValueError:
                        try:
                            obj = json.loads(js_to_json(value), strict=False)
                        except Exception:
                            continue
                    yield from self._u_walk(page, obj, 'data-attrs', group)
                    continue
                kind = _url_kind(value)
                if not kind:
                    if (not _SPECIFIC_ATTR_RE.search(name) or _NON_MEDIA_EXT_RE.search(value)
                            or not value.startswith(('http://', 'https://', '/')) or re.search(r'\s', value)):
                        continue
                    kind = _key_kind(name)
                cand = self._u_candidate(page, value, 'data-attrs', kind=kind, group=group)
                if cand:
                    yield cand

    def _u_check_link_meta(self, run, page):
        """<link rel=preload/alternate> and <meta> tags pointing at media"""
        group = f'{page.prefix}link-meta'
        tags = [extract_attributes(m.group(0)) for m in re.finditer(r'(?is)<(?:link|meta)\b[^>]+>', page.webpage)]
        og_type = next((
            t.get('content') for t in tags
            if (t.get('property') or t.get('name') or '').lower() in ('og:video:type', 'og:audio:type')), None)
        for attrs in tags:
            url = kind = None
            rel = (attrs.get('rel') or '').lower()
            prop = (attrs.get('property') or attrs.get('name') or attrs.get('itemprop') or '').lower()
            if rel and attrs.get('href'):
                if not set(rel.split()) & {'preload', 'prefetch', 'alternate'}:
                    continue
                url = attrs['href']
                kind = _url_kind(url) or _mime_kind(attrs.get('type'))
                if not kind and (attrs.get('as') or '').lower() not in ('video', 'audio'):
                    continue
            elif prop in ('og:video', 'og:video:url', 'og:video:secure_url', 'og:audio', 'og:audio:url',
                          'og:audio:secure_url', 'twitter:player:stream', 'contenturl'):
                url = attrs.get('content')
                kind = _url_kind(url or '')
                if not kind and prop != 'twitter:player:stream' and not _mime_kind(og_type):
                    continue
            if not url:
                continue
            cand = self._u_candidate(page, url, 'link-meta', kind=None if kind == 'http' else kind, group=group)
            if cand:
                cand.kind = cand.kind or _url_kind(cand.url)
                yield cand

    def _u_check_url_scan(self, run, page):
        """Any media-looking URL anywhere in the page source, including escaped/encoded ones"""
        for url in _scan_text(page.webpage):
            cand = self._u_candidate(page, url, 'url-scan')
            if cand:
                yield cand

    def _u_check_linked_resources(self, run, page):
        """Scripts and JSON files the page links to"""
        limit = self._u_int('max_scripts', 8)
        if limit <= 0:
            return
        page_host = urllib.parse.urlparse(page.url).hostname or ''
        ranked, seen = [], set()

        def add(url, bonus):
            full = urllib.parse.urldefrag(urllib.parse.urljoin(page.url, unescapeHTML(url)))[0]
            parsed = urllib.parse.urlparse(full)
            if parsed.scheme not in ('http', 'https') or full in seen:
                return
            seen.add(full)
            host, name = parsed.hostname or '', posixpath.basename(parsed.path)
            if _JUNK_HOST_RE.search(host) or _LIB_SCRIPT_RE.search(parsed.path):
                return
            score = bonus + (2 if host == page_host else 0) + (3 if _GOOD_SCRIPT_RE.search(name) else 0)
            ranked.append((-score, len(ranked), full))

        for src in re.findall(r'''(?is)<script\b[^>]*?\bsrc\s*=\s*["']([^"']+)''', page.webpage):
            add(src, 0)
        for _, url in re.findall(r'''(["'])((?:https?:)?/[^"'\s<>]+?\.json(?:\?[^"'\s<>]*)?)\1''',
                                 _js_unescape(page.webpage)):
            if _GOOD_SCRIPT_RE.search(url):
                add(url, 4)

        for n, (_, _, url) in enumerate(sorted(ranked)[:limit], start=1):
            text = self._u_fetch_text(
                url, page, f'Scanning linked resource {n}: {posixpath.basename(urllib.parse.urlparse(url).path)}')
            if not text:
                continue
            if text.lstrip()[:1] in '{[':
                try:
                    yield from self._u_walk(
                        page, json.loads(text, strict=False), 'linked-resources', f'{page.prefix}res:{n}')
                    continue
                except ValueError:
                    pass
            # third-party bundles are full of non-media endpoints: extensions only
            for found in _scan_text(text, hidden=False):
                cand = self._u_candidate(page, found, 'linked-resources')
                if cand:
                    yield cand

    def _u_fetch_text(self, url, page, note, limit=2 * 1024 * 1024):
        try:
            urlh = self._request_webpage(url, page.video_id, note=note, headers={'Referer': page.url})
        except ExtractorError:
            return None
        try:
            return urlh.read(limit).decode('utf-8', 'replace')
        except Exception:
            return None
        finally:
            urlh.close()

    def _u_check_iframes(self, run, page):
        """Iframes: hand to a site extractor if one wants it, otherwise look inside"""
        max_depth = self._u_int('iframe_depth', 2)
        seen = set()
        for tag in re.findall(r'(?is)<iframe\b[^>]*>', page.webpage):
            attrs = extract_attributes(tag)
            src = attrs.get('src') or attrs.get('data-src') or attrs.get('data-lazy-src')
            if not src or src.startswith(('about:', 'javascript:', 'data:', '#')):
                continue
            iframe_url = urllib.parse.urldefrag(urllib.parse.urljoin(page.url, src))[0]
            parsed = urllib.parse.urlparse(iframe_url)
            if (parsed.scheme not in ('http', 'https') or iframe_url in seen or iframe_url == page.url
                    or _JUNK_HOST_RE.search(parsed.hostname or '') or _WIDGET_IFRAME_RE.search(iframe_url)):
                continue
            seen.add(iframe_url)
            if len(seen) > 6:
                break

            if self._u_claimed(iframe_url):
                yield self.url_result(iframe_url)
                continue
            if page.depth >= max_depth:
                continue
            try:
                urlh = self._request_webpage(
                    iframe_url, page.video_id, note=f'Downloading iframe {len(seen)}',
                    headers={'Referer': page.url})
            except ExtractorError:
                continue
            first = urlh.read(512)
            if not is_html(first):
                urlh.close()
                cand = self._u_candidate(page, iframe_url, 'iframes')
                if cand:
                    yield cand
                continue
            webpage = self._webpage_read_content(
                urlh, iframe_url, page.video_id, prefix=first, fatal=False, errnote=False)
            if not webpage:
                continue

            sub = _Page(urlh.url, webpage, urlh, page.video_id, page.title,
                        depth=page.depth + 1, prefix=f'{page.prefix}iframe{len(seen)}/')
            try:
                nested = list(super()._extract_embeds(
                    iframe_url, webpage, urlh=urlh,
                    info_dict={'id': page.video_id, 'title': page.title, 'age_limit': None}))
            except ExtractorError:
                nested = []
            for entry in nested:
                if entry.get('formats') or entry.get('ie_key') or self._u_claimed(entry.get('url')):
                    yield entry
            yield from self._u_run_checks(run, sub)

    # ---- resolving candidates -------------------------------------------

    def _u_resolve_all(self, run, candidates, known, page):
        unique = {}
        for cand in candidates:
            first = unique.get(cand.url)
            if first is None:
                unique[cand.url] = cand
                continue
            # same URL from another check: keep the first, borrow what it lacks
            first.group = first.group or cand.group
            first.kind = first.kind or cand.kind
            first.hints = {**cand.hints, **{k: v for k, v in first.hints.items() if v is not None}}
            first.meta = {**cand.meta, **{k: v for k, v in first.meta.items() if v is not None}}

        # manifests first, then unknowns, then plain files
        order = {'hls': 0, 'dash': 0, 'ism': 0, 'hds': 1, 'smil': 1, None: 2, 'http': 3}
        queue = sorted(unique.values(), key=lambda c: order.get(c.kind, 2))
        limit = self._u_int('max_candidates', 30)

        items = []
        for n, cand in enumerate(queue):
            stat = run.stat(cand.source)
            if cand.url in known:
                run.rows.append((cand, '-', 0, 'already found by built-in'))
                continue
            if n >= limit:
                run.rows.append((cand, '-', 0, 'skipped (max_candidates)'))
                continue
            item, status = self._u_resolve(run, cand, page.video_id)
            if item:
                items.append(item)
                stat['media'] += 1
                stat['formats'] += len(item.formats)
            run.rows.append((cand, item.kind if item else cand.kind or '?', len(item.formats) if item else 0, status))
        return items

    def _u_resolve(self, run, cand, video_id):
        """Turn a candidate into an _Item. Returns (item or None, status text)"""
        headers = {**({'Referer': cand.referer} if cand.referer else {}), **cand.headers}
        hinted = cand.kind
        if hinted in ('hls', 'dash', 'ism', 'hds', 'smil'):
            item = self._u_manifest_item(run, cand, hinted, video_id, headers)
            if item:
                return item, 'ok'

        self.write_debug(f'Universal sweep: probing {cand.url}')
        try:
            urlh = self._request_webpage(
                cand.url, video_id, note=False, headers={**headers, 'Accept-Encoding': 'identity'})
        except ExtractorError as e:
            status = getattr(e.cause, 'status', None)
            return None, f'HTTP {status}' if status else 'request failed'
        try:
            first = urlh.read(4096)
        except Exception:
            first = b''
        finally:
            urlh.close()
        kind, ext = _sniff(first, urlh.headers.get('Content-Type') or '')
        if not kind:
            return None, ext
        if kind != 'http':
            item = None if kind == hinted else self._u_manifest_item(run, cand, kind, video_id, headers)
            return (item, 'ok') if item else (None, 'no formats in manifest')

        ext = ext or urllib.parse.urlparse(urlh.url).path.rpartition('.')[2].lower() or 'mp4'
        name = posixpath.basename(urllib.parse.urlparse(cand.url).path)
        resolution = parse_resolution(name, lenient=True) if re.search(r'\d{3,4}p|\d+x\d+', name) else {}
        height = cand.hints.get('height') or resolution.get('height')
        is_audio = ext in _AUDIO_EXTS or (urlh.headers.get('Content-Type') or '').lower().startswith('audio/')
        fmt = {
            'url': cand.url,
            'ext': ext,
            'format_id': run.next_id(f'http-{height}p' if height else 'http'),
            'format_note': cand.hints.get('label'),
            'height': height,
            'width': cand.hints.get('width') or resolution.get('width'),
            'tbr': cand.hints.get('tbr'),
            'filesize': int_or_none(urlh.headers.get('Content-Length')),
            'http_headers': dict(headers),
        }
        if is_audio:
            fmt['vcodec'] = 'none'
        return _Item('http', cand.url, [fmt], {}, cand.source, cand.group,
                     info={k: v for k, v in cand.meta.items() if v is not None}), 'ok'

    def _u_manifest_item(self, run, cand, kind, video_id, headers):
        formats, subtitles = [], {}
        try:
            if kind == 'hls':
                formats, subtitles = self._extract_m3u8_formats_and_subtitles(
                    cand.url, video_id, 'mp4', m3u8_id=run.next_id('hls'),
                    headers=headers, fatal=False, errnote=False)
            elif kind == 'dash':
                formats, subtitles = self._extract_mpd_formats_and_subtitles(
                    cand.url, video_id, mpd_id=run.next_id('dash'),
                    headers=headers, fatal=False, errnote=False)
            elif kind == 'ism':
                formats, subtitles = self._extract_ism_formats_and_subtitles(
                    cand.url, video_id, ism_id=run.next_id('mss'),
                    headers=headers, fatal=False, errnote=False)
            elif kind == 'hds':
                formats = self._extract_f4m_formats(
                    cand.url, video_id, f4m_id=run.next_id('hds'), headers=headers, fatal=False)
            elif kind == 'smil':
                formats, subtitles = self._extract_smil_formats_and_subtitles(cand.url, video_id, fatal=False)
        except ExtractorError:
            return None
        if not formats:
            return None
        for fmt in formats:
            if headers:
                fmt['http_headers'] = {**headers, **(fmt.get('http_headers') or {})}
        info = {'id': video_id, 'formats': formats}
        if kind in ('hls', 'dash'):
            # live detection / VOD duration / user-supplied key & query args
            self._extra_manifest_info(info, cand.url)
        info = {k: v for k, v in {**cand.meta, **info}.items() if v is not None and k not in ('id', 'formats')}
        return _Item(kind, cand.url, formats, subtitles or {}, cand.source, cand.group, info=info)

    def _u_probe_siblings(self, run, items, page):
        """Opt-in: ask the server for the same stream in the other manifest formats"""
        have, queue = {i.url for i in items}, []
        for item in items:
            if item.kind not in ('hls', 'dash', 'ism') or not item.url:
                continue
            parsed = urllib.parse.urlparse(item.url)
            path, variants = parsed.path, []
            mobj = re.search(r'(?i)\.isml?/', path)
            if mobj:
                root = path[:mobj.end()]
                variants = [root + 'manifest', root + '.m3u8', root + '.mpd',
                            root + 'manifest(format=m3u8-aapl)', root + 'manifest(format=mpd-time-csf)']
            else:
                stem, _, ext = path.rpartition('.')
                dirname = posixpath.dirname(path)
                if ext.lower() == 'm3u8':
                    variants = [stem + '.mpd', dirname + '/manifest.mpd']
                elif ext.lower() == 'mpd':
                    variants = [stem + '.m3u8', dirname + '/master.m3u8', dirname + '/playlist.m3u8',
                                dirname + '/index.m3u8']
            for variant in variants:
                url = parsed._replace(path=variant).geturl()
                if url in have:
                    continue
                have.add(url)
                queue.append(_Candidate(
                    url, 'sibling-probe', kind=_url_kind(url), group=item.group or f'sibling:{item.url}',
                    referer=page.url, meta=dict(item.info)))
            item.group = item.group or f'sibling:{item.url}'
        found = []
        kinds_by_group = {}
        for item in items:
            kinds_by_group.setdefault(item.group, set()).add(item.kind)
        stat = run.stat('sibling-probe')
        started = time.monotonic()
        for cand in queue:
            stat['found'] += 1
            if cand.kind in kinds_by_group.get(cand.group, ()):
                continue
            item, status = self._u_resolve(run, cand, page.video_id)
            if item:
                found.append(item)
                kinds_by_group.setdefault(cand.group, set()).add(item.kind)
                stat['media'] += 1
                stat['formats'] += len(item.formats)
            run.rows.append((cand, item.kind if item else cand.kind or '?', len(item.formats) if item else 0, status))
        stat['time'] += time.monotonic() - started
        return found

    # ---- grouping and merging -------------------------------------------

    @staticmethod
    def _u_same_video(a, b):
        da, db = a.info.get('duration'), b.info.get('duration')
        durations_match = False
        if da and db:
            if abs(da - db) > max(2, 0.03 * max(da, db)):
                return False
            durations_match = True
        same_group = a.group is not None and a.group == b.group
        related = bool(a.stem and b.stem and a.stem.related(b.stem))
        if a.kind != b.kind:
            # the same video offered over different protocols
            return same_group or durations_match or related
        # same protocol twice: only alternates of one file (qualities, mirrors)
        return bool(a.stem and b.stem and a.stem.base == b.stem.base and (same_group or related))

    def _u_build_entries(self, items, video_id, page_title):
        mode = traverse_obj(self._u_arg('merge'), 0) or 'smart'
        if mode == 'all' or len(items) < 2:
            groups = [items] if items else []
        elif mode == 'none':
            groups = [[item] for item in items]
        else:
            parent = list(range(len(items)))

            def find(i):
                while parent[i] != i:
                    parent[i] = parent[parent[i]]
                    i = parent[i]
                return i

            for i, a in enumerate(items):
                for j in range(i + 1, len(items)):
                    if self._u_same_video(a, items[j]):
                        parent[find(j)] = find(i)
            by_root = {}
            for i, item in enumerate(items):
                by_root.setdefault(find(i), []).append(item)
            groups = list(by_root.values())

            # One HLS, one DASH, one file and nothing tying them together is
            # almost always a single video in three protocols.
            kinds = [{i.kind for i in g} for g in groups]
            durations = {round(i.info['duration']) for g in groups for i in g if i.info.get('duration')}
            if (len(groups) > 1 and sum(map(len, kinds)) == len(set().union(*kinds))
                    and len(durations) <= 1):
                groups = [[i for g in groups for i in g]]

        entries = []
        for n, group in enumerate(groups, start=1):
            entry = dict(next((i.base for i in group if i.base), None) or {})
            formats, subtitles = [], {}
            for item in group:
                formats.extend(item.formats)
                self._merge_subtitles(item.subtitles, target=subtitles)
                for key, value in item.info.items():
                    if value is not None and entry.get(key) is None:
                        entry[key] = value
            self._remove_duplicate_formats(formats)  # works in place
            entry['formats'] = formats
            if subtitles:
                entry['subtitles'] = subtitles
            if len(groups) > 1:
                entry.setdefault('id', f'{video_id}-{n}')
                if not entry.get('title'):
                    entry['title'] = f'{page_title} ({n})'
            entries.append(entry)
        return entries

    # ---- last resort 1: borrow a site extractor's logic -------------------

    def _u_borrow_candidates(self, page):
        """Site extractors whose URL pattern fits this page's path, best first.

        Each extractor's own site comes from its test URLs: the page URL is
        rewritten onto that site and must then match the extractor's pattern.
        A page that mentions the extractor's site (a script, an API, a CDN) is
        the strongest sign of a clone or a white-label copy."""
        parsed = urllib.parse.urlparse(page.url)
        clone_base = _base_host(parsed.hostname)
        html = page.webpage.lower()
        only = self._u_arg('borrow_ies')
        min_literal = self._u_int('borrow_min_path', 4)
        best = {}
        for key, ie in list(self._downloader._ies.items()):
            low = key.lower()
            if low in _BORROW_SKIP or (only is not None and low not in only):
                continue
            try:
                cls = getattr(ie, 'real_class', ie)
                if not cls.working() or cls._VALID_URL is False:
                    continue
                hosts = _ie_hosts(cls)
            except Exception:
                continue
            for scheme, host in hosts:
                base = _base_host(host)
                if not base or base == clone_base:
                    continue
                trial = parsed._replace(scheme=scheme, netloc=host).geturl()
                try:
                    mobj = cls._match_valid_url(trial)
                except Exception:
                    mobj = None
                if not mobj:
                    continue
                literal = self._u_required_chars(cls, trial, len(scheme) + 3 + len(host), mobj.end())
                referenced = base in html
                if only is None and not referenced and literal < min_literal:
                    continue
                whole = mobj.end() >= len(trial) or trial[mobj.end()] in '?#'
                score = literal + (100 if referenced else 0) + (2 if whole else 0)
                if key not in best or score > best[key][0]:
                    best[key] = (score, cls, host, trial, referenced)
        return sorted(best.items(), key=lambda kv: -kv[1][0])

    @staticmethod
    def _u_required_chars(ie, url, start, end):
        r"""How many letters/digits of the path the extractor's pattern really requires.

        Each one is swapped for another of its kind; if the URL then stops matching, the
        pattern spelled it out: "/ft-watch/" counts 7, "(?:/[\w-]+)*" counts 0."""
        path_end = min((i for i in (url.find('?', start), url.find('#', start)) if i >= 0), default=len(url))
        dot = url.rfind('.', url.rfind('/', start, path_end) + 1, path_end)
        if dot >= 0:
            end = min(end, dot)  # a file extension (.html, .php) says little about the site
        count = 0
        for i in range(start, end):
            char = url[i]
            if char.isalpha() and char.isascii():
                alt = 'z' if char.lower() == 'q' else 'q'
                alt = alt.upper() if char.isupper() else alt
            elif char.isdigit():
                alt = '3' if char == '7' else '7'
            else:
                continue
            try:
                if not ie._match_valid_url(url[:i] + alt + url[i + 1:]):
                    count += 1
            except Exception:
                pass
        return count

    def _u_borrow(self, run, page):
        if run.borrow_tried or not self._u_enabled('borrow'):
            return []
        run.borrow_tried = True
        stat, started = run.stat('borrow'), time.monotonic()
        try:
            ranked = self._u_borrow_candidates(page)
        except Exception as e:
            stat['error'] = f'{type(e).__name__}: {e}'
            self.write_debug(f'Universal borrow failed:\n{traceback.format_exc()}')
            ranked = []
        limit, budget = self._u_int('borrow_max', 6), self._u_int('borrow_time', 120)
        if ranked:
            names = ', '.join(key for key, _ in ranked[:limit])
            self._u_say(f'Nothing found on the page; trying the logic of site extractors that fit it: {names}')
        result = []
        for n, (key, (_, cls, host, trial, _referenced)) in enumerate(ranked):
            cand = _Candidate(trial, 'borrow')
            if n >= limit:
                run.rows.append((cand, key, 0, 'skipped (borrow_max)'))
                continue
            if time.monotonic() - started > budget:
                run.rows.append((cand, key, 0, 'skipped (borrow_time)'))
                continue
            stat['found'] += 1
            entries, status = self._u_borrow_one(cls, key, host, trial, page)
            formats = sum(len(e.get('formats') or [1]) for e in entries)
            run.rows.append((cand, key, formats, status))
            if entries and not result:
                stat['media'] += 1
                stat['formats'] += formats
                run.borrowed = key
                result = entries
                break
        stat['time'] += time.monotonic() - started
        return result

    def _u_borrow_one(self, cls, key, host, trial, page):
        """Run one extractor as if the page were on its site. Returns (entries, status)"""
        hostmap = _HostMap(host, page.url)
        self.write_debug(f'Universal borrow: {key} on {trial}; requests for {hostmap.orig} go to {hostmap.netloc}')
        try:
            info = _borrowed_class(cls, hostmap)(self._downloader).extract(trial)
        except Exception as e:  # any failure just means this extractor does not fit
            msg = str(getattr(e, 'orig_msg', None) or e).strip().splitlines()
            return [], (msg[0][:80] if msg else type(e).__name__)
        entries = self._u_borrowed_entries(info, hostmap, page)
        if not entries:
            return [], 'no formats'
        first = traverse_obj(entries, (0, 'entries', 0)) or entries[0]
        ok, status = self._u_verify_formats(first.get('formats') or [first], page.video_id)
        if not ok:
            return [], status
        for entry in entries:
            entry['borrowed_extractor'] = key
        return entries, status

    def _u_borrowed_entries(self, info, hostmap, page):
        """The extractor's result with the original site's URLs pointed at this one; only real media"""
        if not isinstance(info, dict):
            return []
        kind = info.get('_type', 'video')
        if kind in ('url', 'url_transparent'):
            return []  # a pointer elsewhere, not media; following it could loop back here

        def playable(entry):
            return (isinstance(entry, dict) and entry.get('_type', 'video') == 'video'
                    and (entry.get('formats') or entry.get('url')))

        if kind in ('playlist', 'multi_video'):
            try:
                entries = [e for e in itertools.islice(iter(info.get('entries') or []), 100) if playable(e)]
            except Exception:
                entries = []
            if not entries:
                return []
            info = {**info, 'entries': entries}
        elif not playable(info):
            return []
        info = hostmap.deep({k: v for k, v in info.items() if k not in ('webpage_url', 'original_url')})
        return [info]

    def _u_verify_formats(self, formats, video_id):
        """Check that the extractor's formats are real media, not guesses on the wrong site"""
        checkable = [f for f in reversed(formats)
                     if isinstance(f, dict) and not f.get('has_drm') and (f.get('manifest_url') or f.get('url'))]
        if not checkable:
            return True, 'ok (not verified)'
        status = 'formats are not media'
        for fmt in checkable[:2]:
            url = fmt.get('manifest_url') or fmt.get('url')
            headers = {**(fmt.get('http_headers') or {}), 'Accept-Encoding': 'identity'}
            try:
                urlh = self._request_webpage(url, video_id, note=False, headers=headers)
            except ExtractorError as e:
                code = getattr(e.cause, 'status', None)
                status = f'formats unreachable (HTTP {code})' if code else 'formats unreachable'
                continue
            try:
                first = urlh.read(4096)
            except Exception:
                first = b''
            finally:
                urlh.close()
            kind, reason = _sniff(first, urlh.headers.get('Content-Type') or '')
            if kind:
                return True, 'ok'
            status = f'formats are not media ({reason})'
        return False, status

    # ---- last resort 2: a headless browser --------------------------------

    _U_PLAY_SELECTORS = (
        '.vjs-big-play-button', '.jw-display-icon-container', '.plyr__control--overlaid',
        '.fp-play', '.mejs__overlay-button', '.ytp-large-play-button', '.play-button', '.btn-play',
        'button[aria-label*="play" i]', 'button[title*="play" i]', '[class*="play"][class*="button"]',
    )
    _U_NUDGE_JS = '''(selectors) => {
        for (const v of document.querySelectorAll('video, audio')) {
            try { v.muted = true; const p = v.play(); if (p && p.catch) p.catch(() => {}); } catch (e) {}
        }
        for (const s of selectors) {
            const el = document.querySelector(s);
            if (el) { try { el.click(); } catch (e) {} }
        }
        return {
            srcs: Array.from(document.querySelectorAll('video, audio, video source, audio source'))
                .map(el => el.currentSrc || el.src).filter(src => src && /^https?:/.test(src)),
            players: document.querySelectorAll('video, audio, iframe, object, embed').length,
        };
    }'''

    def _u_browser(self, run, page):
        """Open the page in a headless browser and resolve the media requests its player makes"""
        if run.browser_tried or not self._u_enabled('browser'):
            return []
        run.browser_tried = True
        stat, started = run.stat('browser'), time.monotonic()
        try:
            candidates = self._u_browser_capture(page)
        except ImportError:
            stat['error'] = 'Playwright is not installed'
            self.write_debug('Universal browser: Playwright is not installed; skipping')
            candidates = []
        except Exception as e:
            stat['error'] = f'{type(e).__name__}: {str(e).splitlines()[0][:120] if str(e) else ""}'
            self.write_debug(f'Universal browser failed:\n{traceback.format_exc()}')
            candidates = []
        stat['found'] += len(candidates)
        stat['time'] += time.monotonic() - started
        if not candidates:
            return []
        items = self._u_resolve_all(run, candidates, set(), page)
        return self._u_build_entries(items, page.video_id, page.title)

    def _u_browser_capture(self, page):
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import sync_playwright

        wait = self._u_int('browser_wait', 20)
        channel = traverse_obj(self._u_arg('browser_channel'), 0)
        endpoint = os.environ.get('DLP_BROWSER_CDP')
        user_agent = traverse_obj(self._downloader.params, ('http_headers', 'User-Agent'))
        page_base = _base_host(urllib.parse.urlparse(page.url).hostname)
        seen = {}  # url -> kind guess, in the order the player asked for them

        def covers_page(domain):
            domain = _base_host(domain.lstrip('.'))
            return page_base == domain or page_base.endswith('.' + domain)

        def note(url, kind):
            if not url.startswith(('http://', 'https://')) or url in seen:
                return
            host = urllib.parse.urlparse(url).hostname or ''
            if _JUNK_HOST_RE.search(host) or _NON_MEDIA_EXT_RE.search(url):
                return
            seen[url] = kind

        def on_request(req):
            kind = _url_kind(req.url)
            if kind or req.resource_type == 'media':
                note(req.url, kind)

        def on_response(resp):
            kind = _mime_kind((resp.headers.get('content-type') or '').split(';')[0])
            if kind:
                note(resp.url, kind)

        self._u_say(f'Still nothing; opening the page in a headless browser (up to {wait} s)')
        with sync_playwright() as pw:
            if endpoint:
                browser = pw.chromium.connect_over_cdp(endpoint)
            else:
                browser, last_error = None, None
                for option in ([channel] if channel else [None, 'chrome', 'msedge']):
                    try:
                        browser = pw.chromium.launch(headless=True, **({'channel': option} if option else {}))
                        break
                    except PlaywrightError as e:
                        last_error = e
                if browser is None:
                    raise ExtractorError(
                        'No browser for Playwright: run "playwright install chromium" or install Chrome',
                        cause=last_error, expected=True)
            try:
                context = browser.new_context(**({'user_agent': user_agent} if user_agent else {}))
                # the user's cookies for this site (--cookies, --cookies-from-browser) carry logins over
                cookies = [{
                    'name': c.name, 'value': c.value or '', 'domain': c.domain, 'path': c.path or '/',
                    'secure': bool(c.secure), **({'expires': float(c.expires)} if c.expires else {}),
                } for c in self.cookiejar if c.domain and covers_page(c.domain)]
                if cookies:
                    with contextlib.suppress(PlaywrightError):
                        context.add_cookies(cookies)
                tab = context.new_page()
                # pop-ups (ads opened by a click) are closed straight away
                context.on('page', lambda popup: popup.close() if popup != tab else None)
                tab.on('request', on_request)
                tab.on('response', on_response)
                try:
                    tab.goto(page.url, wait_until='domcontentloaded', timeout=wait * 1000)
                except PlaywrightError as e:
                    self.write_debug(f'Universal browser: page load: {e}')
                begun = time.monotonic()
                deadline, nudged, settle, players = begun + wait, 0, None, 0
                while time.monotonic() < deadline:
                    manifests = [u for u, k in seen.items() if k in ('hls', 'dash', 'ism', 'hds')]
                    if (manifests or seen) and settle is None:
                        settle = time.monotonic() + (2 if manifests else 4)  # collect alternates
                    if settle and time.monotonic() > settle:
                        break
                    if nudged < 3 and time.monotonic() > begun + 2 + 3 * nudged:
                        nudged += 1
                        for frame in tab.frames:
                            try:
                                found = frame.evaluate(self._U_NUDGE_JS, list(self._U_PLAY_SELECTORS))
                            except PlaywrightError:
                                continue
                            players += found.get('players') or 0
                            for src in found.get('srcs') or []:
                                note(src, _url_kind(src) or 'http')
                    elif nudged == 3 and not seen and not players:
                        break  # no player anywhere on the page and nothing requested: stop early
                    tab.wait_for_timeout(500)
                try:
                    title = tab.title()
                except PlaywrightError:
                    title = None
                jar_cookies = context.cookies()
            finally:
                browser.close()

        # cookies the page set (tokens) are needed to fetch what it requested
        for c in jar_cookies:
            self._set_cookie(c['domain'], c['name'], c['value'], path=c.get('path') or '/',
                             secure=bool(c.get('secure')), expire_time=int(c['expires']) if c.get('expires', -1) > 0 else None)
        if title and page.title in (None, page.video_id, 'video'):
            page.title = title
        return [_Candidate(url, 'browser', kind=kind, referer=page.url) for url, kind in seen.items()]

    # ---- report ----------------------------------------------------------

    @staticmethod
    def _u_dump_report(run, url, error):
        """Append a JSON line describing this run to $YTDLP_UNIVERSAL_REPORT (used by dlp-add-site)"""
        path = os.environ.get('YTDLP_UNIVERSAL_REPORT')
        if not path:
            return
        record = {
            'url': unsmuggle_url(url, {})[0],
            'site': run.site,
            'swept': run.swept,
            'builtin': run.builtin,
            'redirect_to': run.redirect_to,
            'error': error,
            'borrowed': run.borrowed,
            'checks': run.stats,
            'candidates': [
                {'check': cand.source, 'url': cand.url, 'kind': kind, 'formats': count, 'result': status}
                for cand, kind, count, status in run.rows],
        }
        try:
            with open(path, 'a', encoding='utf-8') as f:
                f.write(json.dumps(record) + '\n')
        except OSError:
            pass

    def _u_print_report(self, run):
        if not self._u_flag('report'):
            return
        if not run.swept:
            if run.builtin:
                self._u_say('Universal sweep not run: the built-in checks already matched. '
                            'Use "universal:URL" or --extractor-args "generic:sweep" to force it')
            elif run.builtin is None and not run.redirect_to:
                self._u_say('Universal sweep not run: the URL itself is media, a manifest or a feed')
            return

        names = ['builtin', *(name for name, _ in self._U_CHECKS), 'sibling-probe', 'borrow', 'browser']
        rows = []
        for name in names:
            stat = run.stats.get(name)
            if not stat:
                rows.append([name, '', '', '', '', 'skipped'])
                continue
            status = stat['error'] or ('ok' if stat['found'] else 'nothing')
            if name == 'builtin':
                rows.append([name, str(stat['found']), '', '', f'{stat["time"] * 1000:.0f} ms', status])
                continue
            rows.append([name, str(stat['found']), str(stat['media']), str(stat['formats']),
                         f'{stat["time"] * 1000:.0f} ms', status])
        self._u_say('Universal sweep report\n' + render_table(
            ['CHECK', 'FOUND', 'MEDIA', 'FORMATS', 'TIME', 'STATUS'], rows, delim='-', extra_gap=1))

        if run.rows:
            def short(url):
                return url if len(url) <= 96 else f'{url[:60]}...{url[-33:]}'
            self._u_say('Candidates\n' + render_table(
                ['CHECK', 'KIND', 'FMTS', 'RESULT', 'URL'],
                [[cand.source, kind, str(count), status, short(cand.url)]
                 for cand, kind, count, status in run.rows],
                delim='-', extra_gap=1))


__all__ = ['UniversalIE', 'UniversalSiteIE']
