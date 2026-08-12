# Setup on a raw machine

Movies Nerd intentionally has a small dependency surface. It does not install software by itself.

This is a one-time technical reference for installation or troubleshooting. Do not repeat its addresses, ports, commands, or configuration terms during a normal movie request. Once configured, Movies Nerd opens qBittorrent itself and keeps these details out of the user conversation.

## Required software

1. Python 3.11 or newer. All bundled Python scripts use only the standard library.
2. qBittorrent 5.x, desktop or `qbittorrent-nox`, for BitTorrent metadata and transfers.
3. FFmpeg and ffprobe for media inspection and lossless container remuxing.
4. Git only when developing or updating the skill repository.

Optional: MKVToolNix provides `mkvpropedit`, which can normalize track headers in seconds without a complete remux. Movies Nerd uses it only when available and when the staging filesystem supports a copy-on-write rollback clone; FFmpeg remains the dependency-free fallback path.

No Node.js, npm packages, Docker image, browser extension, cloned scraper server, or Python package is required.

## Configure fast search

No search key or account is required. `scripts/search_releases.py` queries YTS, Knaben, APIBay, and Magnetz concurrently under one shared deadline, then falls back to EXT only when necessary. Exact IMDb lookup and validated YTS direct `.torrent` metadata form the movie fast path. It uses the operating system's `curl` only if Python's TLS trust store is broken.

For broader coverage, optionally configure Jackett and add its Torznab feeds to qBittorrent's search engine. qBittorrent recommends Jackett, but Movies Nerd never installs it automatically. Keep Jackett on `127.0.0.1`, configure only the indexers you trust, and store its generated API key in qBittorrent rather than this repository.

Provider credentials never block a normal request. Movies Nerd does not offer or request subtitle API keys.

## Configure subtitles

No subtitle account or API key is required. Movies Nerd uses Stremio's official OpenSubtitles v3 service directly, without opening a subtitle website. If the user has voluntarily provided an OpenSubtitles API key, it may optionally be exposed through the current shell or an approved secret manager:

```sh
export OPENSUBTITLES_API_KEY='<your key from opensubtitles.com>'
```

Do not put the real key in this repository, a media filename, NFO, command-line argument, or provider URL. When no key is configured, Movies Nerd silently continues through `scripts/stremio_subtitles.py`. It never asks the user for a key.

## Suggested manual installation

Review commands before running them and use the package manager already trusted on the machine.

### macOS with Homebrew

```sh
brew install --cask qbittorrent
brew install ffmpeg python git
```

The existing desktop qBittorrent application is sufficient; `qbittorrent-nox` is not required on macOS.

For the optional fast MKV header path:

```sh
brew install mkvtoolnix
```

### Debian or Ubuntu

```sh
sudo apt update
sudo apt install python3 ffmpeg qbittorrent-nox git
```

For the optional fast MKV header path:

```sh
sudo apt install mkvtoolnix
```

### Windows

Install current releases from the official Python, FFmpeg, Git, and qBittorrent sites. Add Python and FFmpeg to `PATH`.

## Configure qBittorrent safely

1. Open qBittorrent settings and enable the Web User Interface.
2. Bind the Web UI to `127.0.0.1`, not `0.0.0.0`, a LAN address, or a public interface.
3. Use port `8080` or set `QBITTORRENT_URL` to the chosen loopback URL.
4. Create a unique Web UI username and password. Do not enable unauthenticated localhost bypass.
5. Do not expose the Web UI through router port forwarding, UPnP, a public reverse proxy, or a tunnel.
6. Supply credentials only in the current shell or an approved secret manager:

```sh
export QBITTORRENT_URL='http://127.0.0.1:8080'
export QBITTORRENT_USERNAME='your-local-user'
export QBITTORRENT_PASSWORD='your-local-password'
```

Never commit those values, put them in magnet URLs, or paste them into metadata files.

## Prepare the library paths

Ask only for the library path needed by the current request, and only when the user has not already supplied or established it. Folder names are descriptive, not mandatory: `/Volumes/ssd/Films` is a valid movie-library root and must not become `/Volumes/ssd/Films/Movies`. Configure whichever path is relevant for the current shell:

```sh
export MOVIES_NERD_MOVIES_ROOT='/absolute/path/to/Movies'
export MOVIES_NERD_SERIES_ROOT='/absolute/path/to/Series'
```

If the user chooses the defaults, use `~/Documents/Movies` and `~/Documents/Series`. Staging is always `.incoming/Movies Nerd` inside the applicable selected root. When both roots are known they must be distinct, non-nested directories; never use a home directory, filesystem root, or shared volume root as a library root. Do not require a path merely because the unrelated library root is unknown.

Create or mount the selected roots before the first transfer. Movies Nerd creates only the hidden staging subdirectory when an authorized transfer is prepared.

## Verify the machine

From the installed skill directory, run:

```sh
python3 scripts/check_environment.py
python3 scripts/qbittorrent_api.py status
python3 scripts/subtitle_provider.py --title 'Example' --year 2024
python3 -m unittest discover -s tests -v
```

The checker is read-only. Normal acquisition opens qBittorrent automatically. A `needs-local-app-access` status means the execution host must approve the local-app command; retry it through that host mechanism instead of telling the user the app is closed.
