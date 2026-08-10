# Movies Nerd

Movies Nerd is an opinionated, all-in-one movie acquisition and library-maintenance skill for Codex. It searches and ranks user-authorized releases, optimizes comparable encodes by runtime-normalized size, hands approved magnets to a local qBittorrent instance in a stopped state, verifies size and payload contents, skips extras by default, prefers an efficient 4K release under 15 GiB or a strong space-efficient 1080p fallback, monitors stalled transfers and proposes a different-source replacement, remuxes compatible media to MKV without re-encoding, and organizes movies and series with artwork, NFO metadata, English and French subtitles, and clean track labels.

It is designed around a user-selected pair of library roots:

- Movies: `<MOVIES_ROOT>/<Director>/<Title> (<Year>)/`
- Series: `<SERIES_ROOT>/<Series> (<Year>)/Season NN/`
- Final video names include only the title/year or episode identity plus `[2160p]`, `[1080p]`, or another resolution tag.

Movies Nerd asks for both roots before library work. If the user does not specify them, the defaults are `~/Documents/Movies` and `~/Documents/Series`; no shared drive or volume layout is assumed.

OpenSubtitles is used when `OPENSUBTITLES_API_KEY` is configured. Without a key, Movies Nerd asks once and then falls back to an exact-match Subtitle Cat browser download, followed by local SRT and timing validation.

## Safety and dependencies

The skill is a clean-room implementation. It does not run the third-party torrent-search project that inspired the idea. Its scripts use the Python standard library and existing command-line tools only. Required software is Python 3.11+, qBittorrent 5.x, and FFmpeg/ffprobe. Node.js, npm, Docker, cloned scraper servers, and automatic installers are intentionally absent.

Downloads require separate selection and start confirmations, begin in a user-private hidden staging directory, and use qBittorrent's loopback-only Web API. A simple two-gate model rejects unsafe metadata before content transfer, then checks real file signatures and media structure once after transfer. Renamed executables, scripts, archives, disk images, spoofed paths, symlinks, invalid companions, and non-media files fail closed. The API client can add, inspect, start, and stop a torrent, but cannot delete one. `sandbox-exec` is used as optional defense in depth on macOS; media hashes and checksum manifests are intentionally absent.

## Setup

Read [`references/setup.md`](references/setup.md), select the library roots, enable qBittorrent's Web UI on `127.0.0.1`, and provide credentials through `QBITTORRENT_USERNAME` and `QBITTORRENT_PASSWORD`. Then run:

```sh
python3 scripts/check_environment.py
python3 scripts/qbittorrent_api.py status
```

The scripts never install dependencies or change qBittorrent settings automatically.

## Usage

Install the `movies-nerd` folder as a Codex skill, then ask Codex to use `$movies-nerd`. The authoritative workflow is in [`SKILL.md`](SKILL.md); library conventions and security controls are in [`references/`](references/).

## License

MIT — see [`LICENSE`](LICENSE).
