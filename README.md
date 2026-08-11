# Movies Nerd

Movies Nerd is an opinionated, all-in-one Codex skill for film fans. A specific-title request is treated as authorization; it shows quality, size, peers, and source, then asks once before downloading.

## Features

- Picks an efficient 4K release up to 15 GiB, or a space-efficient 1080p fallback.
- Searches concurrent no-key APIs first and opens EXT only when the fast path finds nothing usable.
- Uses local qBittorrent, prepares a different-source backup, monitors stalls incrementally, skips extras, and prefers MKV without re-encoding or needless rewrites.
- Creates clean movie/series folders with posters, NFO metadata, track labels, and English/French subtitles. It uses OpenSubtitles with `OPENSUBTITLES_API_KEY`, or Subtitle Cat without one.
- Prepares metadata, artwork, subtitles, and recommendations while the transfer runs so finalization stays short.
- Suggests a similar film and another by the director, with verified Letterboxd and SensCritique links.

## Layout

- Movies: `<MOVIES_ROOT>/<Director>/<Title> (<Year>)/`
- Series: `<SERIES_ROOT>/<Series> (<Year>)/Season NN/`
- Videos: title/year or episode name plus a resolution tag such as `[1080p]`.

Roots are selected separately. Defaults are `~/Documents/Movies` and `~/Documents/Series`; no shared drive layout is assumed.

## Setup

Requires Python 3.11+, qBittorrent 5.x, and FFmpeg/ffprobe. Optional MKVToolNix enables fast header-only edits. Scripts use Python's standard library—no Node.js, npm, Docker, scraper server, or automatic installer.

Follow [setup](references/setup.md), enable qBittorrent's Web UI on `127.0.0.1`, set `QBITTORRENT_USERNAME` and `QBITTORRENT_PASSWORD`, then run:

```sh
python3 scripts/check_environment.py
python3 scripts/qbittorrent_api.py status
```

## Safety

This clean-room project runs no third-party scraper code. Downloads use private staging, loopback-only qBittorrent, and two checks that reject dangerous files, archives, spoofed paths, symlinks, and invalid media. The client cannot delete torrents. Optional macOS sandboxing adds isolation; media hashes and checksum manifests are omitted. See [security policy](references/security-policy.md).

## Use

Install the `movies-nerd` folder and ask Codex to use `$movies-nerd`. The full workflow is in [SKILL.md](SKILL.md), with details under [references](references/).

MIT licensed — see [LICENSE](LICENSE).
