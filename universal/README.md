# yt-dlp-universal

A yt-dlp extractor plugin that sweeps a page for every media format it can find.

yt-dlp's built-in generic extractor stops at the first thing it recognises and
gives up with "Unsupported URL" when media is tucked away in JSON state, a
player config, an encoded string, a linked script or an iframe. This plugin
keeps the built-in checks, adds a pipeline of extra ones, identifies what each
found URL really is by its content, and merges all formats of a video into one
entry.

When even that finds nothing, two last resorts run before it gives up:

1. **Borrow** the logic of yt-dlp's site extractors that fit the page: run them
   as if the page were on their site (see [Borrowing](#borrowing)).
2. **Open the page in a headless browser**, let the player start, and use the
   media requests it makes (see [Headless browser](#headless-browser)).

DRM is not touched.

## Install

It is built into dlp: installing the fork installs the plugin (`yt_dlp_plugins/extractor/universal.py`)
and the `dlp-add-site` command (`dlp_tools/add_site.py`).

```bash
uv tool install "yt-dlp[default] @ git+https://github.com/thatbraxguy/dlp"
```

`dlp -v --version` should list `Extractor Plugins: UniversalIE, UniversalSiteIE, universal (GenericIE)`.
Don't also put a copy in a yt-dlp plugin folder (`~/.config/yt-dlp/plugins/`): dlp would
load both and they clash.

The headless-browser step needs Playwright, an optional extra. It drives an installed
Google Chrome or Microsoft Edge; without one, also run `playwright install chromium`.

```bash
uv tool install --reinstall "yt-dlp[default,browser] @ git+https://github.com/thatbraxguy/dlp"
```

## Use

| You type | What happens |
|---|---|
| `dlp URL` | Site extractors as usual. If none matches, the built-in generic checks run; **only if they find nothing** does the sweep run, then borrowing, then the browser. |
| `dlp universal:URL` | Generic + the full sweep, always, even for URLs a site extractor would claim. Results of both are merged. |
| `dlp --extractor-args "generic:sweep" URL` | Like the prefix, but only for URLs that reach the generic extractor anyway. |

`-F` then lists everything found (`hls-…`, `dash-…`, `http-…`) and normal format
selection applies.

## Adding a site: `dlp-add-site`

Give the script one page of a site that has a video on it:

```bash
dlp-add-site "https://example.com/watch/123"
```

It runs dlp against the page with increasingly thorough settings (standard
sweep with borrowing and the headless browser, browser impersonation if the
site blocks the request, deep sweep, probing for the other manifest format),
keeps the first that finds media, and stores those settings for the whole site in
`~/.config/yt-dlp/universal-sites.json`. From then on plain `dlp URL` for any
page of that site goes straight to the sweep with those settings. Run it as
often as you like, once per site; run it again for the same site to re-learn it.

| Command | Does |
|---|---|
| `dlp-add-site URL` | Analyse the page and add or update its site |
| `dlp-add-site --dry-run URL` | Analyse and report, save nothing |
| `dlp-add-site --merge all URL` | Also store a merge mode (`all`, `none`, `smart`) for the site |
| `dlp-add-site --site example.com URL` | Add the whole domain, not just the page's host |
| `dlp-add-site --force URL` | Add even if yt-dlp already handles the page |
| `dlp-add-site URL -- --cookies-from-browser firefox` | Pass options to dlp for the analysis (e.g. a login) |
| `dlp-add-site --list` | Show added sites and their settings |
| `dlp-add-site --check` | Re-test every added site with its example page |
| `dlp-add-site --remove example.com` | Forget a site |

If borrowing found the media, the extractor it borrowed from is stored
(`borrow_ies`) and tried first, and alone, for every page of the site. If only
the browser found it, the site goes straight to the browser (`browser_first`).

It changes nothing when yt-dlp has its own extractor for the page, or when the
page already works and the sweep finds nothing more. When nothing works it says
why (no media URL in the page, which extractors were tried, what the browser
saw, server refused, DRM) and leaves any existing entry alone.

The sites file is plain JSON (`{"sites": {"example.com": {"args": {...}}}}`,
`args` being the options below); the previous version is kept as `.bak`.

## Options

Pass with `--extractor-args "generic:KEY=VALUE;KEY2"` (`universal:` is accepted
as an alias for `generic:`).

| Key | Default | Meaning |
|---|---|---|
| `sweep` | off | Run the sweep even when a built-in check already matched |
| `report` | off | Print which check found what, and what each candidate turned out to be |
| `merge` | `smart` | `smart`: group sources into videos; `all`: one video; `none`: one entry per source |
| `checks` | all | Comma-separated list of checks to run |
| `skip_checks` | none | Comma-separated list of checks to skip |
| `max_candidates` | 30 | Most candidate URLs to probe per page |
| `max_scripts` | 8 | Most linked scripts/JSON files to fetch (0 disables) |
| `iframe_depth` | 2 | How many iframe levels to look into |
| `probe_siblings` | off | For a found manifest, also ask for the same stream as HLS/DASH/Smooth |
| `borrow_ies` | any that fit | Comma-separated extractor names to borrow from (and only those) |
| `borrow_max` | 6 | Most extractors to try |
| `borrow_time` | 120 | Seconds after which no further extractor is tried |
| `borrow_min_path` | 4 | Letters/digits of the path an extractor's pattern must spell out, unless the page names its site |
| `browser_wait` | 20 | Seconds to wait for the player in the browser |
| `browser_channel` | auto | `chrome`, `msedge` or `chromium`; auto tries Playwright's Chromium, then Chrome, then Edge |

`borrow` and `browser` are checks like the others, so `skip_checks=borrow,browser` turns
the last resorts off and `checks=...` without them leaves them out. To use a browser
running elsewhere (for example a browserless container), set `DLP_BROWSER_CDP` to its
CDP endpoint (`ws://host:3000`).

## Checks

Run in this order; the first check to find a URL owns it in the report.

| Check | Looks at |
|---|---|
| `builtin` | Everything yt-dlp's generic extractor does (embeds, HTML5 media, JW Player, video.js, JSON-LD, Open Graph…) |
| `json-state` | `__NEXT_DATA__`, Next.js flight data, Nuxt payloads, `<script type=application/json>`, JSON assigned in scripts |
| `player-config` | Player setup literals (`sources: […]`, `.setup({…})`, `new Plyr/Hls/…`) and `loadSource()` / `initialize()` / `.src` calls |
| `data-attrs` | `data-*` attributes holding a URL or a JSON config |
| `link-meta` | `<link rel=preload/alternate>`, `og:video`, `twitter:player:stream` |
| `url-scan` | Any media-looking URL in the source, including `\/`-escaped, `/`-escaped, percent-encoded and base64 forms |
| `linked-resources` | Scripts and `.json` files the page links to (known libraries and trackers are skipped) |
| `iframes` | Hands the iframe to a site extractor if one wants it, otherwise fetches it and sweeps it |
| `sibling-probe` | Opt-in. `x.m3u8` ↔ `x.mpd`, `x.ism/manifest` ↔ `.m3u8` / `.mpd` |
| `borrow` | Only if nothing was found. Site extractors that fit the page, run as if the page were on their site |
| `browser` | Only if still nothing. The media requests a headless browser sees while the page plays |

Each candidate is then **resolved**: fetched and identified by its first bytes
(`#EXTM3U`, `<MPD`, `<SmoothStreamingMedia`, MP4/WebM/Ogg/MP3/FLAC magic), so a
manifest with no extension or the wrong one still works and HTML is discarded.
Formats come from yt-dlp's own HLS/DASH/ISM/HDS/SMIL parsers.

## Borrowing

Many sites are copies of others: white-label platforms, clones of a tube script,
a broadcaster's regional sites. yt-dlp may have an extractor for the original
whose logic would work on the copy, but its URL pattern names the original's
domain, so it never sees the copy.

1. **Which extractors fit.** Each extractor's own site comes from its test URLs.
   The page URL is moved onto that site (same path and query) and must then match
   the extractor's pattern. A fit only counts if the pattern really spells out
   part of the path (at least `borrow_min_path` letters/digits, file extension
   not counted), or if the page mentions that site anywhere (a script, an API, a
   CDN), which is the strongest sign of a copy. Best fits are tried first.
2. **Running one.** The extractor runs on the moved URL, but every request it
   makes to its own site (and its subdomains, when the page has a real domain
   name) goes to the page's site instead, cookies included. URLs of the original
   site in its result are pointed at the page's site too. It never logs in:
   credentials for the original site are never sent anywhere else.
3. **Accepting it.** Only real media counts: a result that just points to
   another URL is ignored, and one or two formats are fetched and must be media
   (by their first bytes). The entry gets `borrowed_extractor` set.

`-v` shows each try; the report lists every extractor tried and why it failed.
Most fail in milliseconds, at their first regular expression.

## Headless browser

When nothing else worked, the page is opened in headless Chrome (Playwright) with
yt-dlp's user agent and your cookies for the site (so `--cookies-from-browser`
logins carry over). It clicks the usual play buttons in every frame, starts any
`<video>`, closes pop-ups, and records requests that are media by URL, by type
or by response `Content-Type` (ad and tracker hosts are ignored). It stops a
couple of seconds after the first media appears, after `browser_wait` seconds,
or early if the page has no player at all. Cookies the page set are kept for the
download. The recorded URLs then go through the same resolving and merging as
everything else, so `-F` shows HLS/DASH/file formats as usual.

## How sources become videos (`merge=smart`)

Two sources are treated as the same video when

- they use different protocols and came from the same object/element, or share
  an id-like path part or directory, or have the same duration; or
- they use the same protocol and are alternates of one file (`clip_720.mp4` /
  `clip_1080.mp4`, or the same manifest on two hosts).

Durations that differ veto a merge. If a page yields exactly one source per
protocol and nothing contradicts it, they are taken to be one video. Several
videos become a playlist. Wrong guess? Use `merge=all` or `merge=none`.

## Report

```
CHECK             FOUND  MEDIA  FORMATS  TIME    STATUS
builtin           0                      210 ms  nothing
json-state        3      3      4        1 ms    ok
url-scan          3      0      0        0 ms    ok
```

`FOUND` is how many candidates the check produced; `MEDIA` / `FORMATS` count
only candidates it found first that resolved to real media. A second table
lists every candidate with its result (`ok`, `html page`, `HTTP 403`, …).

## Tests

From the repository root:

```bash
universal/tests/make_fixtures.sh
```

```bash
python3 universal/tests/test_universal.py && python3 universal/tests/test_add_site.py
```

The first builds tiny MP4/HLS/DASH clips with ffmpeg; the second serves the
fixture pages on localhost and runs this repository's yt_dlp (`python -m yt_dlp`) against each
(`-v` shows yt-dlp's output, a word filters cases by name).

The borrow cases use a test-only extractor (`tests/fixtures/plugins`, "FakeTube")
and clones of its pages. The browser cases run only when the Python running the
tests has Playwright:

```bash
uv run --no-project --with playwright python universal/tests/test_universal.py
```

## Adding a check

A check is a method on `_UniversalGenericIE` taking `(run, page)` and yielding
`_Candidate`s (use `self._u_candidate(page, url, 'my-check', kind=…, group=…)`),
registered in `_U_CHECKS`. Resolving, grouping, merging and reporting are
shared. The last resorts (`_u_borrow`, `_u_browser`) run after the pipeline.
