"""
dlp-add-site: teach dlp's universal plugin a new site.

    dlp-add-site URL            analyse a page and add its site
    dlp-add-site --list         show the sites added so far
    dlp-add-site --check        re-test every added site with its example page
    dlp-add-site --remove SITE  forget a site

Give it one page of the site that has a video on it. It runs yt-dlp against the
page with increasingly thorough settings, keeps the first that finds media, and
stores them for the whole site in ~/.config/yt-dlp/universal-sites.json. From
then on plain `yt-dlp <any page of that site>` uses the universal sweep with
those settings. Run it again for the same site to re-learn it.

Options after `--` go to yt-dlp during the analysis, e.g. a login session:
    dlp-add-site URL -- --cookies-from-browser firefox
"""

import argparse
import datetime
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import urllib.parse

RUN_TIMEOUT = 240
PLAYLIST_LIMIT = 25

# Tried in order; the first that finds media wins. Options are the plugin's
# extractor-args (see the README), stored as lists of strings.
LADDER = [
    ('standard sweep', {}, False),
    ('standard sweep, pretending to be a browser', {'impersonate': ['']}, True),
    ('deep sweep (more scripts, more candidates, deeper iframes)',
     {'max_scripts': ['25'], 'max_candidates': ['80'], 'iframe_depth': ['3']}, False),
    ('deep sweep, pretending to be a browser',
     {'max_scripts': ['25'], 'max_candidates': ['80'], 'iframe_depth': ['3'], 'impersonate': ['']}, True),
]
KIND_NAMES = {'hls': 'HLS', 'dash': 'DASH', 'smooth': 'Smooth', 'http': 'file'}


def sites_path():
    explicit = os.environ.get('YTDLP_UNIVERSAL_SITES')
    if explicit:
        return pathlib.Path(explicit)
    config = os.environ.get('XDG_CONFIG_HOME') or os.path.join(os.path.expanduser('~'), '.config')
    return pathlib.Path(config, 'yt-dlp', 'universal-sites.json')


def load_sites(path):
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return {'version': 1, 'sites': {}}
    except (OSError, ValueError) as e:
        sys.exit(f'Cannot read {path}: {e}\nFix or delete that file, then run this again.')
    if not isinstance(data, dict) or not isinstance(data.get('sites'), dict):
        sys.exit(f'{path} is not a sites file (no "sites" object). Fix or delete it, then run this again.')
    return data


def save_sites(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        shutil.copy2(path, path.with_suffix(path.suffix + '.bak'))
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    os.replace(tmp, path)


def domain_of(text):
    """example.com from a URL or a bare host; a leading www. is dropped"""
    parsed = urllib.parse.urlparse(text if '://' in text else f'//{text}')
    host = (parsed.hostname or '').lower()
    return host[4:] if host.startswith('www.') else host


def covers(domain, host):
    return host == domain or host.endswith('.' + domain)


class Result:
    def __init__(self, returncode, info, stderr, reports):
        self.returncode, self.stderr, self.reports = returncode, stderr, reports
        self.entries = []
        self.top = info.get('extractor_key') if isinstance(info, dict) else None
        self._collect(info)
        self.media = [e for e in self.entries if e['kinds']]
        self.ok = bool(self.media)
        self.extractors = sorted({e['extractor'] for e in self.media if e['extractor']})
        self.kinds = sorted({k for e in self.media for k in e['kinds']})
        self.formats = sum(sum(e['kinds'].values()) for e in self.media)
        self.drm = 'DRM' in stderr or any(e['drm'] for e in self.entries)
        self.blocked = any(marker in stderr for marker in ('HTTP Error 403', 'Cloudflare', 'HTTP Error 429'))

    def _collect(self, node):
        if not isinstance(node, dict):
            return
        if node.get('_type') == 'playlist':
            for entry in node.get('entries') or []:
                self._collect(entry)
            return
        kinds, drm, height = {}, 0, 0
        for fmt in node.get('formats') or []:
            if fmt.get('has_drm'):
                drm += 1
                continue
            protocol = fmt.get('protocol') or ''
            kind = ('hls' if protocol.startswith('m3u8') else 'dash' if 'dash' in protocol
                    else 'smooth' if protocol == 'ism' else 'http')
            kinds[kind] = kinds.get(kind, 0) + 1
            height = max(height, fmt.get('height') or 0)
        self.entries.append({
            'title': node.get('title'), 'id': node.get('id'), 'extractor': node.get('extractor_key'),
            'kinds': kinds, 'drm': drm, 'height': height,
        })

    @property
    def swept(self):
        return [r for r in self.reports if r.get('swept')]

    def finders(self):
        """Names of the checks whose candidates turned into media"""
        names = []
        for report in self.swept:
            for cand in report.get('candidates') or []:
                if cand.get('result') == 'ok' and cand.get('check') not in names:
                    names.append(cand['check'])
        return names

    def describe(self):
        if not self.ok:
            return 'nothing'
        parts = []
        for entry in self.media[:4]:
            kinds = ', '.join(f'{KIND_NAMES.get(k, k)} x{n}' for k, n in sorted(entry['kinds'].items()))
            height = f', up to {entry["height"]}p' if entry['height'] else ''
            parts.append(f'"{(entry["title"] or entry["id"] or "?")[:50]}" ({kinds}{height})')
        more = f' and {len(self.media) - 4} more' if len(self.media) > 4 else ''
        count = '1 video' if len(self.media) == 1 else f'{len(self.media)} videos'
        return f'{count}: ' + '; '.join(parts) + more


def run_ytdlp(ytdlp, url, sites, extra, workdir, verbose):
    """Run yt-dlp -J on url with the given sites table. Returns a Result"""
    sites_file, report_file = workdir / 'sites.json', workdir / 'report.jsonl'
    sites_file.write_text(json.dumps({'version': 1, 'sites': sites}), encoding='utf-8')
    report_file.unlink(missing_ok=True)
    env = {**os.environ, 'YTDLP_UNIVERSAL_SITES': str(sites_file), 'YTDLP_UNIVERSAL_REPORT': str(report_file)}
    cmd = [*ytdlp, '--no-warnings', '-J', '--playlist-end', str(PLAYLIST_LIMIT), *extra, url]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=RUN_TIMEOUT)
        returncode, stdout, stderr = proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired as e:
        returncode, stdout = 124, ''
        stderr = f'{(e.stderr or b"").decode("utf-8", "replace") if isinstance(e.stderr, bytes) else e.stderr or ""}'
        stderr += f'\nERROR: timed out after {RUN_TIMEOUT} seconds'
    if verbose and stderr.strip():
        print('      | ' + stderr.strip().replace('\n', '\n      | '))
    info = None
    if stdout.strip():
        try:
            info = json.loads(stdout)
        except ValueError:
            pass
    reports = []
    if report_file.exists():
        for line in report_file.read_text(encoding='utf-8').splitlines():
            try:
                reports.append(json.loads(line))
            except ValueError:
                pass
    return Result(returncode, info, stderr, reports)


def last_error(result):
    lines = [line for line in result.stderr.splitlines() if line.startswith('ERROR')]
    return lines[-1][:300] if lines else ''


def explain_failure(results, extra):
    """Say, in plain words, why no strategy worked and what could be tried"""
    swept = [report for result in results for report in result.swept]
    candidates = max((report.get('candidates') or [] for report in swept), key=len, default=[])
    print()
    if any(result.drm for result in results):
        print('The streams on this page are DRM-protected. yt-dlp cannot download those and this')
        print('tool does not work around it.')
        return
    if not swept:
        if any(result.blocked for result in results):
            print('The site refused the page request itself (HTTP 403/429 or a Cloudflare challenge),')
            print('even when pretending to be a browser, so there was nothing to analyse.')
        else:
            print('The page could not be analysed:')
            print('  ' + (last_error(results[-1]) or 'yt-dlp gave no usable output'))
        if not extra:
            print('If the page needs a login, retry with your browser session:')
            print('  dlp-add-site URL -- --cookies-from-browser firefox')
        return
    if not candidates:
        print('No media URL was found in the page, its JSON, its scripts or its iframes.')
        print('The player most likely builds the stream URL in JavaScript or asks an API for it.')
        print('A static scan cannot see that; this site needs a hand-written extractor.')
        return
    print(f'{len(candidates)} possible media URLs were found, but none turned out to be playable:')
    for cand in candidates[:8]:
        url = cand['url'] if len(cand['url']) <= 90 else cand['url'][:60] + '...' + cand['url'][-27:]
        print(f'  {cand.get("result", "?"):<28} {url}')
    refused = [c for c in candidates if str(c.get('result', '')).startswith('HTTP 4')]
    if refused and not extra:
        print('Some were refused by the server. If the site needs a login, retry with your session:')
        print('  dlp-add-site URL -- --cookies-from-browser firefox')


def add_site(args, ytdlp, extra):
    url = args.url if '://' in args.url else 'https://' + args.url
    domain = args.site and domain_of(args.site) or domain_of(url)
    host = domain_of(url)
    if not domain or '.' not in domain and domain != 'localhost':
        sys.exit(f'Cannot work out a site from {args.url!r}')
    if not covers(domain, host):
        sys.exit(f'--site {domain} does not cover {host}')

    path = sites_path()
    data = load_sites(path)
    updating = domain in data['sites']
    # the tests below must not be influenced by what is already stored for this host
    others = {d: p for d, p in data['sites'].items() if not covers(d, host)}
    base_args = {'merge': [args.merge]} if args.merge else {}

    print(f'Site: {domain}' + ('  (already added; learning it again)' if updating else ''))
    results = []
    with tempfile.TemporaryDirectory(prefix='dlp-add-site-') as tmp:
        workdir = pathlib.Path(tmp)

        print('  1. yt-dlp without a site entry ...', flush=True)
        stock = run_ytdlp(ytdlp, url, others, extra, workdir, args.verbose)
        print(f'     {stock.describe() if stock.ok else last_error(stock) or "nothing"}')
        # the plugin writes a report whenever the generic extractor handles a URL;
        # media without one means a site-specific extractor did the work
        native = not stock.reports
        if stock.ok and native and not args.force:
            print(f'\nyt-dlp already supports this page with its own {stock.top or "site"} extractor.')
            print('Nothing was changed. Use --force to route the site through the universal sweep anyway.')
            return 0

        chosen = None
        blocked = stock.blocked
        step = 1
        for label, strategy, needs_block in LADDER:
            if needs_block and not blocked:
                continue
            step += 1
            print(f'  {step}. {label} ...', flush=True)
            trial = {**base_args, **strategy}
            result = run_ytdlp(ytdlp, url, {**others, domain: {'args': trial}}, extra, workdir, args.verbose)
            results.append(result)
            if not result.reports:
                print('\nThe universal plugin did not run. Check that `yt-dlp -v` lists')
                print('"Extractor Plugins: UniversalIE, UniversalSiteIE, universal (GenericIE)".')
                return 1
            blocked = blocked or result.blocked
            print(f'     {result.describe() if result.ok else last_error(result) or "nothing"}')
            if result.ok:
                chosen = (label, trial, result)
                break

        if not chosen:
            explain_failure([stock, *results], extra)
            print(f'\n{domain} was not added.' + (' Its existing entry was left as it is.' if updating else ''))
            return 1
        label, site_args, result = chosen

        # worth asking the server for the other manifest format?
        if {'hls', 'dash'} & set(result.kinds) and not {'hls', 'dash'} <= set(result.kinds):
            step += 1
            print(f'  {step}. same, also probing for the other manifest format ...', flush=True)
            trial = {**site_args, 'probe_siblings': ['']}
            probed = run_ytdlp(ytdlp, url, {**others, domain: {'args': trial}}, extra, workdir, args.verbose)
            if probed.ok and set(probed.kinds) > set(result.kinds):
                print(f'     {probed.describe()}')
                site_args, result = trial, probed
            else:
                print('     nothing extra')

    # an explicit --merge is a setting the user wants stored, gain or not
    if (stock.ok and result.formats <= stock.formats and set(result.kinds) <= set(stock.kinds)
            and not args.force and not args.merge):
        print('\nyt-dlp already gets everything from this page that the sweep finds.')
        print('Nothing was changed. Use --force to add the site anyway.')
        return 0

    profile = {
        'args': site_args,
        'example': url,
        'added': datetime.date.today().isoformat(),
        'strategy': label,
        'found': {'videos': len(result.media), 'kinds': result.kinds, 'checks': result.finders()},
    }
    if extra:
        profile['analysed_with'] = extra
    print()
    if args.dry_run:
        print(f'Dry run: would save this for {domain}:')
        print('  ' + json.dumps(profile, indent=2).replace('\n', '\n  '))
        return 0
    data['sites'][domain] = profile
    save_sites(path, data)
    print(f'{"Updated" if updating else "Added"} {domain}  ({path})')
    print(f'  how:    {label}; found by {", ".join(result.finders()) or "the built-in checks"}')
    print(f'  result: {result.describe()}')
    if len(result.media) > 1:
        print('  Several videos were found. If they are really one, re-run with --merge all;')
        print('  if sources of different videos got mixed, re-run with --merge none.')
    if extra:
        print(f'  The analysis used: {" ".join(extra)}  (pass the same to yt-dlp when downloading)')
    print(f'\nNow this works for any page on {domain}:')
    print(f'  yt-dlp "{url}"')
    return 0


def list_sites():
    path = sites_path()
    sites = load_sites(path)['sites']
    if not sites:
        print(f'No sites added yet ({path}).')
        return 0
    width = max(map(len, sites))
    for domain, profile in sorted(sites.items()):
        options = '; '.join(
            key if values in ([''], ['true']) else f'{key}={",".join(map(str, values))}'
            for key, values in sorted((profile.get('args') or {}).items())) or 'default options'
        found = '+'.join((profile.get('found') or {}).get('kinds') or []) or '?'
        print(f'{domain:<{width}}  {profile.get("added", "?"):<10}  {found:<14}  {options}')
        if profile.get('example'):
            print(f'{"":<{width}}  example: {profile["example"]}')
    return 0


def remove_site(name):
    path = sites_path()
    data = load_sites(path)
    domain = domain_of(name)
    if domain not in data['sites']:
        print(f'{domain} is not in {path}. Added sites: {", ".join(sorted(data["sites"])) or "none"}')
        return 1
    del data['sites'][domain]
    save_sites(path, data)
    print(f'Removed {domain}. yt-dlp handles it the stock way again.')
    return 0


def check_sites(names, ytdlp, extra, verbose):
    data = load_sites(sites_path())
    wanted = [domain_of(n) for n in names] or sorted(data['sites'])
    failed = 0
    with tempfile.TemporaryDirectory(prefix='dlp-add-site-') as tmp:
        for domain in wanted:
            profile = data['sites'].get(domain)
            if not profile or not profile.get('example'):
                print(f'{domain}: not added, or no example page stored')
                failed += 1
                continue
            result = run_ytdlp(ytdlp, profile['example'], data['sites'], extra, pathlib.Path(tmp), verbose)
            print(f'{domain}: {"ok - " + result.describe() if result.ok else "BROKEN - " + (last_error(result) or "nothing found")}')
            failed += not result.ok
    if failed:
        print('\nRe-run `dlp-add-site URL` for a broken site to learn it again.')
    return 1 if failed else 0


def _main():
    argv, extra = sys.argv[1:], []
    if '--' in argv:
        split = argv.index('--')
        argv, extra = argv[:split], argv[split + 1:]
    parser = argparse.ArgumentParser(
        prog='dlp-add-site', description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('url', nargs='?', help='a page of the site that has a video on it')
    parser.add_argument('--list', action='store_true', help='show the sites added so far')
    parser.add_argument('--check', nargs='*', metavar='SITE', help='re-test added sites (all if none named)')
    parser.add_argument('--remove', metavar='SITE', help='forget a site')
    parser.add_argument('--site', metavar='DOMAIN', help='add this domain instead of the page\'s exact host')
    parser.add_argument('--merge', choices=('smart', 'all', 'none'), help='store a merge mode for the site')
    parser.add_argument('--force', action='store_true', help='add the site even if yt-dlp already handles the page')
    parser.add_argument('--dry-run', action='store_true', help='analyse and report, but save nothing')
    parser.add_argument('-v', '--verbose', action='store_true', help='show yt-dlp\'s own output for every step')
    args = parser.parse_args(argv)

    if args.list:
        return list_sites()
    if args.remove:
        return remove_site(args.remove)
    # The yt_dlp this script was installed with (dlp), never whatever `yt-dlp` is on PATH
    ytdlp = [sys.executable, '-m', 'yt_dlp']
    if args.check is not None:
        return check_sites(args.check, ytdlp, extra, args.verbose)
    if not args.url:
        parser.print_usage()
        return 2
    return add_site(args, ytdlp, extra)


def main():
    try:
        sys.exit(_main())
    except KeyboardInterrupt:
        sys.exit('\nInterrupted; nothing was saved.')


if __name__ == '__main__':
    main()
