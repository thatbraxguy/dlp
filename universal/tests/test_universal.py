#!/usr/bin/env python3
"""
End-to-end tests: serve the fixture pages on localhost and run the real
yt_dlp from this repository (python -m yt_dlp, plugin included) against each one.

    ./tests/make_fixtures.sh        # once, needs ffmpeg
    python3 tests/test_universal.py [-v] [name ...]
"""

import base64
import functools
import importlib.util
import http.server
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import threading
import urllib.parse

ROOT = pathlib.Path(__file__).resolve().parent.parent
REPO = ROOT.parent  # the dlp checkout: yt_dlp, yt_dlp_plugins, dlp_tools
YTDLP = [sys.executable, '-m', 'yt_dlp']
FIXTURES = ROOT / 'tests' / 'fixtures'
VERBOSE = '-v' in sys.argv[1:]
ONLY = [a for a in sys.argv[1:] if not a.startswith('-')]
# An empty config home, so an installed copy of this plugin is not loaded as well
ISOLATED_CONFIG = tempfile.mkdtemp(prefix='yt-dlp-universal-test-')
# This checkout, plus the test-only FakeTube extractor the borrow cases lend from
PYTHONPATH = os.pathsep.join((str(REPO), str(FIXTURES / 'plugins')))
# Browser cases need Playwright in the Python that runs yt-dlp, e.g.
#   uv run --with playwright python universal/tests/test_universal.py
HAS_PLAYWRIGHT = importlib.util.find_spec('playwright') is not None


class Handler(http.server.SimpleHTTPRequestHandler):
    """Static files, with tokens in pages expanded to URLs on this server"""
    requests = []

    def log_message(self, *args):
        pass

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        Handler.requests.append(path)
        if not path.startswith('/pages/'):
            return super().do_GET()
        file = FIXTURES / path.lstrip('/')
        if not file.is_file():
            return self.send_error(404)
        base = f'http://127.0.0.1:{self.server.server_port}'
        text = file.read_text()
        text = re.sub(r'__ESC\((.+?)\)__', lambda m: (base + m.group(1)).replace('/', '\\/'), text)
        text = re.sub(r'__B64\((.+?)\)__', lambda m: base64.b64encode((base + m.group(1)).encode()).decode(), text)
        text = re.sub(r'__PCT\((.+?)\)__', lambda m: urllib.parse.quote(base + m.group(1), safe=''), text)
        body = text.encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/javascript' if file.suffix == '.js' else 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class Server(http.server.ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        # a browser dropping a media request half-way is normal, not a failure
        if not isinstance(sys.exc_info()[1], (BrokenPipeError, ConnectionResetError)):
            super().handle_error(request, client_address)


def run_ytdlp(url, *args):
    # the plugin from this checkout, and never the user's own added sites
    env = {**os.environ, 'PYTHONPATH': PYTHONPATH, 'YTDLP_UNIVERSAL_SITES': os.devnull,
           'XDG_CONFIG_HOME': ISOLATED_CONFIG}
    proc = subprocess.run(
        [*YTDLP, '--ignore-config', '--no-warnings', '-J', *args, url],
        capture_output=True, text=True, env=env, timeout=120)
    info = None
    if proc.stdout.strip():
        try:
            info = json.loads(proc.stdout)
        except ValueError:
            pass
    return proc, info


def kinds(entry):
    out = set()
    for fmt in entry.get('formats') or []:
        protocol = fmt.get('protocol') or ''
        out.add('hls' if protocol.startswith('m3u8') else 'dash' if 'dash' in protocol else 'http')
    return out


def entries_of(info):
    if info is None:
        return []
    if info.get('_type') == 'playlist':
        return [e for e in info.get('entries') or [] if e]
    return [info]


# name, page (or callable giving a URL), extra yt-dlp args, expected list of kind-sets (None = must fail), extra check
CASES = [
    ('json-state: extensionless HLS in __NEXT_DATA__', 'json_state.html', [], [{'hls'}],
     lambda info, _: info.get('title') == 'State Video' or f'title was {info.get("title")!r}'),
    ('url-scan: JSON-escaped URL in a script', 'escaped.html', [], [{'hls'}], None),
    ('url-scan: base64 URL', 'base64.html', [], [{'dash'}], None),
    ('url-scan: percent-encoded URL in a query string', 'percent.html', [], [{'dash'}], None),
    ('data-attrs: extensionless HLS in data-stream-url', 'data_attr.html', [], [{'hls'}], None),
    ('player-config: hls.loadSource() call', 'player_call.html', [], [{'hls'}], None),
    ('player-config: unparseable setup() literal, HLS + MP4 merged', 'player_setup.html', [], [{'hls', 'http'}], None),
    ('linked-resources: URL only in a linked script; library script not fetched', 'linked_js.html', [], [{'dash'}],
     lambda _, reqs: '/pages/assets/jquery.min.js' not in reqs or 'jquery.min.js was fetched'),
    ('iframes: media inside an iframe page', 'iframe.html', [], [{'hls'}], None),
    ('link-meta: <link rel=alternate type=mpegurl>', 'link_meta.html', [], [{'hls'}], None),
    ('merge: one video in three protocols -> one entry', 'multi_format.html', [], [{'hls', 'dash', 'http'}],
     lambda info, _: info.get('title') == 'Multi Video' or f'title was {info.get("title")!r}'),
    ('grouping: two videos stay two entries', 'two_videos.html', [], [{'hls', 'http'}, {'hls', 'http'}],
     lambda info, _: [e.get('title') for e in info['entries']] == ['First Video', 'Second Video']
     or f'titles were {[e.get("title") for e in info["entries"]]}'),
    ('merge=all: two videos forced into one entry', 'two_videos.html',
     ['--extractor-args', 'generic:merge=all'], [{'hls', 'http'}], None),
    ('merge=none: every source its own entry', 'multi_format.html',
     ['--extractor-args', 'generic:merge=none'], [{'hls'}, {'dash'}, {'http'}], None),
    ('fallback: built-in hit is returned untouched', 'builtin_html5.html', [], [{'http'}], None),
    ('prefix: universal: adds the sweep to the built-in hit', 'universal:builtin_html5.html', [], [{'hls', 'http'}], None),
    ('sweep arg: same as prefix', 'builtin_html5.html', ['--extractor-args', 'generic:sweep'], [{'hls', 'http'}], None),
    ('skip_checks: disabling the only matching check fails', 'data_attr.html',
     ['--extractor-args', 'generic:skip_checks=data-attrs'], None, None),
    ('checks: restricting to another check fails', 'data_attr.html',
     ['--extractor-args', 'generic:checks=json-state'], None, None),
    ('sibling-probe off: only HLS', 'sibling.html', [], [{'hls'}], None),
    ('sibling-probe on: HLS + DASH', 'sibling.html',
     ['--extractor-args', 'generic:probe_siblings'], [{'hls', 'dash'}], None),
    ('nothing: page without media is still unsupported', 'nothing.html', [], None, None),
    ('direct manifest URL: unchanged generic path', '/media/vid10001/hls/master.m3u8', [], [{'hls'}], None),
    ('direct file URL: unchanged generic path', '/media/vid10001/clip_180p.mp4', [], [{'http'}], None),
    ('borrow: a clone of a site extractor\'s site, which the page names', '/ft-watch/10001', [], [{'http'}],
     lambda info, reqs: (info.get('borrowed_extractor') == 'FakeTube' and info.get('title') == 'Clone Video One') or f'borrowed_extractor={info.get("borrowed_extractor")!r} title={info.get("title")!r}'),
    ('borrow: the clone\'s API is asked, not the original site\'s', '/ft-watch/10001', [], [{'http'}],
     lambda _, reqs: '/api/video/10001.json' in reqs or f'requests were {reqs}'),
    ('borrow: page does not name the site; the path pattern alone is enough', '/ft-watch/30003', [], [{'http'}],
     lambda info, reqs: (info.get('borrowed_extractor') == 'FakeTube' and info.get('title') == 'Clone Video Three') or f'borrowed_extractor={info.get("borrowed_extractor")!r} title={info.get("title")!r}'),
    ('borrow: an extractor that does not fit the page fails cleanly', '/ft-watch/20002', [], None, None),
    ('borrow off: skip_checks=borrow', '/ft-watch/10001',
     ['--extractor-args', 'generic:skip_checks=borrow,browser'], None, None),
    ('borrow_ies: only the named extractor is tried', '/ft-watch/10001',
     ['--extractor-args', 'generic:borrow_ies=faketube'], [{'http'}], lambda info, reqs: (info.get('borrowed_extractor') == 'FakeTube' and info.get('title') == 'Clone Video One') or f'borrowed_extractor={info.get("borrowed_extractor")!r} title={info.get("title")!r}'),
    ('browser: stream URL assembled by JavaScript and fetched', 'browser_fetch.html', [], [{'hls'}], None),
    ('browser: <video> src set by JavaScript', 'browser_video.html', [], [{'http'}], None),
    ('browser off: skip_checks=browser', 'browser_fetch.html',
     ['--extractor-args', 'generic:skip_checks=browser'], None, None),
    ('report: table printed on stderr', 'multi_format.html',
     ['--extractor-args', 'generic:report'], [{'hls', 'dash', 'http'}],
     lambda info, reqs, stderr='': True),
]


def main():
    if not (FIXTURES / 'media').is_dir():
        sys.exit('Run tests/make_fixtures.sh first')
    server = Server(
        ('127.0.0.1', 0), functools.partial(Handler, directory=str(FIXTURES)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f'http://127.0.0.1:{server.server_port}'

    failed = 0
    for name, page, args, expected, extra in CASES:
        if ONLY and not any(o in name for o in ONLY):
            continue
        if name.startswith('browser:') and not HAS_PLAYWRIGHT:
            print(f'skip  {name}  (Playwright not installed)')
            continue
        prefix, _, page = page.rpartition(':')
        url = base + (page if page.startswith('/') else f'/pages/{page}')
        if prefix:
            url = f'{prefix}:{url}'
        Handler.requests.clear()
        proc, info = run_ytdlp(url, *args)
        problems = []
        got = [kinds(e) for e in entries_of(info)]
        if expected is None:
            if proc.returncode == 0:
                problems.append(f'expected failure, got {got}')
            elif 'Unsupported URL' not in proc.stderr:
                problems.append(f'failed for the wrong reason: {proc.stderr.strip()[-300:]}')
        else:
            if proc.returncode != 0:
                problems.append(f'yt-dlp exited {proc.returncode}: {proc.stderr.strip()[-400:]}')
            elif got != expected:
                problems.append(f'expected {expected}, got {got}')
            elif extra and name.startswith('report'):
                if 'Universal sweep report' not in proc.stderr or 'Candidates' not in proc.stderr:
                    problems.append('no report on stderr')
            elif extra:
                verdict = extra(info, list(Handler.requests))
                if verdict is not True:
                    problems.append(str(verdict))
        failed += bool(problems)
        print(f'{"FAIL" if problems else "ok  "}  {name}')
        for problem in problems:
            print(f'        {problem}')
        if VERBOSE or problems:
            for entry in entries_of(info):
                print('        entry:', entry.get('id'), '|', entry.get('title'), '|',
                      sorted(f.get('format_id') for f in entry.get('formats') or []))
        if VERBOSE and proc.stderr.strip():
            print('        ' + proc.stderr.strip().replace('\n', '\n        '))

    server.shutdown()
    print(f'\n{failed} failed' if failed else '\nall passed')
    sys.exit(1 if failed else 0)


if __name__ == '__main__':
    main()
