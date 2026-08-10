# Movies Nerd

Movies Nerd is an opinionated, all-in-one movie acquisition and library-maintenance skill for Codex. It searches and ranks user-authorized releases, hands approved magnets to a local qBittorrent instance in a stopped state, verifies size and payload contents, skips extras by default, prefers a compact 4K release under 15 GiB or a strong 1080p fallback, monitors stalled transfers and proposes a different-source replacement, remuxes compatible media to MKV without re-encoding, and organizes movies and series with artwork, NFO metadata, English and French subtitles, clean track labels, and SHA-256 manifests.

It is designed around one personal library layout:

- Movies: `/Volumes/ssd/Films/<Director>/<Title> (<Year>)/`
- Series: `/Volumes/ssd/Séries/<Series> (<Year>)/Season NN/`
- Final video names include only the title/year or episode identity plus `[2160p]`, `[1080p]`, or another resolution tag.

## Safety and dependencies

The skill is a clean-room implementation. It does not run the third-party torrent-search project that inspired the idea. Its scripts use the Python standard library and existing command-line tools only. Required software is Python 3.11+, qBittorrent 5.x, and FFmpeg/ffprobe. Node.js, npm, Docker, cloned scraper servers, and automatic installers are intentionally absent.

The audit that led to this design is recorded in [`SECURITY_AUDIT.md`](SECURITY_AUDIT.md).

Downloads require a separate confirmation, begin in a hidden staging directory, and use qBittorrent's loopback-only Web API. The API client can add, inspect, start, and stop a torrent, but cannot delete one. Magnet data and downloaded filenames are treated as untrusted. `sandbox-exec` is used as optional defense in depth on macOS.

This project is for public-domain, freely licensed, or otherwise legally authorized media only.

## Setup

Read [`references/setup.md`](references/setup.md), enable qBittorrent's Web UI on `127.0.0.1`, and provide credentials through `QBITTORRENT_USERNAME` and `QBITTORRENT_PASSWORD`. Then run:

```sh
python3 scripts/check_environment.py
python3 scripts/qbittorrent_api.py status
```

The scripts never install dependencies or change qBittorrent settings automatically.

## Usage

Install the `movies-nerd` folder as a Codex skill, then ask Codex to use `$movies-nerd`. The authoritative workflow is in [`SKILL.md`](SKILL.md); library conventions and security controls are in [`references/`](references/).

## License

No license has been selected yet. All rights reserved by default.
