# yt-dlp-universal

A yt-dlp extractor plugin that sweeps a page for every media format it can find.

yt-dlp's built-in generic extractor stops at the first thing it recognises and
gives up with "Unsupported URL" when media is tucked away in JSON state, a
player config, an encoded string, a linked script or an iframe. This plugin
keeps the built-in checks, adds a pipeline of extra ones, identifies what each
found URL really is by its content, and merges all formats of a video into one
entry.

Static analysis only: no JavaScript is executed and no browser is used. DRM is
not touched.

## Install

It is built into dlp: installing the fork installs the plugin (`yt_dlp_plugins/extractor/universal.py`)
and the `dlp-add-site` command (`dlp_tools/add_site.py`).

```bash
uv tool install "yt-dlp[default] @ git+https://github.com/thatbraxguy/dlp"
```

`dlp -v --version` should list `Extractor Plugins: UniversalIE, UniversalSiteIE, universal (GenericIE)`.
Don't also put a copy in a yt-dlp plugin folder (`~/.config/yt-dlp/plugins/`): dlp would
load both and they clash.

## Use

| You type | What happens |
|---|---|
| `dlp URL` | Site extractors as usual. If none matches, the built-in generic checks run; **only if they find nothing** does the sweep run. |
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
sweep, browser impersonation if the site blocks the request, deep sweep,
probing for the other manifest format), keeps the first that finds media, and
stores those settings for the whole site in
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

It changes nothing when yt-dlp has its own extractor for the page, or when the
page already works and the sweep finds nothing more. When nothing works it says
why (no media URL in the page, server refused, DRM) and leaves any existing
entry alone. What it cannot do is write new extraction logic: a site whose
player gets its stream from an API call needs a hand-written extractor.

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

Each candidate is then **resolved**: fetched and identified by its first bytes
(`#EXTM3U`, `<MPD`, `<SmoothStreamingMedia`, MP4/WebM/Ogg/MP3/FLAC magic), so a
manifest with no extension or the wrong one still works and HTML is discarded.
Formats come from yt-dlp's own HLS/DASH/ISM/HDS/SMIL parsers.

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

## Adding a check

A check is a method on `_UniversalGenericIE` taking `(run, page)` and yielding
`_Candidate`s (use `self._u_candidate(page, url, 'my-check', kind=…, group=…)`),
registered in `_U_CHECKS`. Resolving, grouping, merging and reporting are
shared. A headless-browser check that yields the media requests a page makes
would slot in the same way.
