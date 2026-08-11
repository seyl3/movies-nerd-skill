# Movies Nerd

Movies Nerd is an opinionated, all-in-one Codex skill for nontechnical film fans. No torrent or command-line knowledge is expected: it shows one good option, asks once, stays with the download until it is organized, then returns film links and recommendations.

Every new task identifies itself once as `Movies Nerd vX.Y.Z`, so users always know which installed release they are talking to. `VERSION` is the single source of truth, releases use SemVer tags, and `python3 scripts/skill_version.py` prints the exact installed version.

## Features

- Picks an efficient 4K release up to 15 GiB, or a space-efficient 1080p fallback.
- Searches concurrent no-key APIs first and opens EXT only when the fast path finds nothing usable.
- Opens and uses local qBittorrent automatically, downloads only the main movie or episodes, stays through completion, replaces stalled downloads with approval, and prefers MKV without needless conversion.
- Creates clean movie/series folders with posters, NFO metadata, track labels, and English/French subtitles. It uses OpenSubtitles with `OPENSUBTITLES_API_KEY`, or Subtitle Cat without one.
- Prepares fresh metadata, artwork, subtitles, and recommendations while the transfer runs, salvages verified video when separate release junk appears, and clears completed jobs from `.incoming`.
- Suggests a similar film and another by the director, with verified Letterboxd and SensCritique links.

## Layout

- Movies: `<MOVIES_ROOT>/<Director>/<Title> (<Year>)/`
- Series: `<SERIES_ROOT>/<Series> (<Year>)/Season NN/`
- Videos: title/year or episode name plus a resolution tag such as `[1080p]`.

Movies and series roots are selected independently when needed. Their folder names are unrestricted—`/Volumes/ssd/Films` is a valid movie root. Optional defaults are `~/Documents/Movies` and `~/Documents/Series`; no shared drive layout is assumed.

## Setup

Requires Python 3.11+, qBittorrent 5.x, and FFmpeg/ffprobe. Optional MKVToolNix makes some metadata edits faster. Scripts use Python's standard library—no Node.js, npm, Docker, or scraper server. See the one-time [setup guide](references/setup.md) only when installing on a new machine.

## Safety

This clean-room project runs no third-party scraper code. Downloads use private staging, a local-only qBittorrent connection, main-media-only selection, and two checks that reject deceptive or invalid video. Separate junk is discarded and trusted sidecars are rebuilt. Cleanup is narrow and recoverable; the client cannot delete torrents. See [security policy](references/security-policy.md).

## Use

Install the `movies-nerd` folder and ask Codex to use `$movies-nerd`. The full workflow is in [SKILL.md](SKILL.md), with details under [references](references/).

MIT licensed — see [LICENSE](LICENSE).
