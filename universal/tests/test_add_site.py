#!/usr/bin/env python3
"""
End-to-end tests for dlp-add-site (dlp_tools/add_site.py) against the fixture server.

    python3 tests/test_add_site.py [-v]
"""

import functools
import http.server
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import threading

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_universal import FIXTURES, ISOLATED_CONFIG, REPO, YTDLP, Handler, entries_of, kinds  # noqa: E402

VERBOSE = '-v' in sys.argv[1:]
failures = []


def check(name, condition, detail=''):
    print(f'{"ok  " if condition else "FAIL"}  {name}')
    if not condition:
        failures.append(name)
        if detail:
            print('        ' + str(detail).strip().replace('\n', '\n        '))


def main():
    server = http.server.ThreadingHTTPServer(
        ('127.0.0.1', 0), functools.partial(Handler, directory=str(FIXTURES)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_port
    ip, name = f'http://127.0.0.1:{port}', f'http://localhost:{port}'

    with tempfile.TemporaryDirectory() as tmp:
        sites_file = pathlib.Path(tmp, 'sites.json')
        env = {**os.environ, 'PYTHONPATH': str(REPO), 'YTDLP_UNIVERSAL_SITES': str(sites_file),
               'XDG_CONFIG_HOME': ISOLATED_CONFIG}

        def script(*args):
            proc = subprocess.run([sys.executable, '-m', 'dlp_tools.add_site', *args], capture_output=True, text=True, env=env)
            if VERBOSE:
                print('    $ dlp-add-site ' + ' '.join(args))
                print('    ' + (proc.stdout + proc.stderr).strip().replace('\n', '\n    '))
            return proc

        def sites():
            return json.loads(sites_file.read_text())['sites'] if sites_file.exists() else {}

        def ytdlp(url):
            proc = subprocess.run([*YTDLP, '--ignore-config', '--no-warnings', '-J', url],
                                  capture_output=True, text=True, env=env)
            info = json.loads(proc.stdout) if proc.stdout.strip() not in ('', 'null') else None
            return [kinds(e) for e in entries_of(info)]

        proc = script(f'{ip}/pages/nothing.html')
        check('page without media: exits 1, saves nothing', proc.returncode == 1 and not sites(), proc.stdout)
        check('  ...and explains why', 'No media URL was found' in proc.stdout, proc.stdout)

        proc = script(f'{ip}/pages/plain_html5.html')
        check('page stock yt-dlp fully handles: nothing added',
              proc.returncode == 0 and not sites() and 'already gets everything' in proc.stdout, proc.stdout)

        proc = script('--dry-run', f'{ip}/pages/builtin_html5.html')
        check('--dry-run: reports but saves nothing',
              proc.returncode == 0 and not sites() and 'Dry run' in proc.stdout, proc.stdout)

        check('before adding: built-in hit only', ytdlp(f'{ip}/pages/builtin_html5.html') == [{'http'}])
        proc = script(f'{ip}/pages/builtin_html5.html')
        check('add site 1 (sweep finds more than stock)', proc.returncode == 0 and '127.0.0.1' in sites(), proc.stdout)
        check('  plain URL now gets the swept formats', ytdlp(f'{ip}/pages/builtin_html5.html') == [{'hls', 'http'}])
        check('  other pages of the site work too', ytdlp(f'{ip}/pages/json_state.html') == [{'hls'}])
        check('  direct media on the site still works', ytdlp(f'{ip}/media/vid10001/clip_180p.mp4') == [{'http'}])

        proc = script(f'{name}/pages/sibling.html')
        saved = sites().get('localhost', {})
        check('add site 2: both sites kept', proc.returncode == 0 and set(sites()) == {'127.0.0.1', 'localhost'},
              proc.stdout)
        check('  learned that probing siblings pays off', 'probe_siblings' in saved.get('args', {}), saved)
        check('  site 2 uses its own options', ytdlp(f'{name}/pages/sibling.html') == [{'hls', 'dash'}])
        check('  site 1 unaffected by site 2 options', ytdlp(f'{ip}/pages/sibling.html') == [{'hls'}])

        proc = script('--merge', 'all', f'{ip}/pages/two_videos.html')
        check('re-add site 1 with --merge all: updated in place',
              proc.returncode == 0 and sites()['127.0.0.1']['args'].get('merge') == ['all']
              and 'localhost' in sites(), proc.stdout)
        check('  stored merge mode applies', ytdlp(f'{ip}/pages/two_videos.html') == [{'hls', 'http'}])

        before = sites()
        proc = script(f'{ip}/pages/nothing.html')
        check('failed re-add leaves the existing entry alone', proc.returncode == 1 and sites() == before, proc.stdout)

        proc = script('--list')
        check('--list shows both', '127.0.0.1' in proc.stdout and 'localhost' in proc.stdout, proc.stdout)
        proc = script('--check')
        check('--check passes for both', proc.returncode == 0 and proc.stdout.count(': ok') == 2, proc.stdout)
        proc = script('--remove', 'localhost')
        check('--remove drops one site', proc.returncode == 0 and set(sites()) == {'127.0.0.1'}, proc.stdout)
        check('  removed site is back to stock', ytdlp(f'{name}/pages/builtin_html5.html') == [{'http'}])
        check('a .bak of the previous file is kept', sites_file.with_suffix('.json.bak').exists())

    server.shutdown()
    print(f'\n{len(failures)} failed' if failures else '\nall passed')
    sys.exit(1 if failures else 0)


if __name__ == '__main__':
    main()
