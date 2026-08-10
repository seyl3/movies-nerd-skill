# Setup on a raw machine

Movies Nerd intentionally has a small dependency surface. It does not install software by itself.

## Required software

1. Python 3.11 or newer. All bundled Python scripts use only the standard library.
2. qBittorrent 5.x, desktop or `qbittorrent-nox`, for BitTorrent metadata and transfers.
3. FFmpeg and ffprobe for media inspection and lossless container remuxing.
4. Git only when developing or updating the skill repository.

Optional on macOS: `/usr/bin/sandbox-exec`. The launcher uses it as defense in depth when present. It is deprecated by Apple and is not a complete security boundary.

No Node.js, npm packages, Docker image, browser extension, cloned scraper server, or Python package is required.

## Suggested manual installation

Review commands before running them and use the package manager already trusted on the machine.

### macOS with Homebrew

```sh
brew install --cask qbittorrent
brew install ffmpeg python git
```

The existing desktop qBittorrent application is sufficient; `qbittorrent-nox` is not required on macOS.

### Debian or Ubuntu

```sh
sudo apt update
sudo apt install python3 ffmpeg qbittorrent-nox git
```

### Windows

Install current releases from the official Python, FFmpeg, Git, and qBittorrent sites. Add Python and FFmpeg to `PATH`. The macOS sandbox launcher is unavailable, but the Python safety checks still apply.

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

The opinionated defaults are:

- Movies: `/Volumes/ssd/Films`
- Series: `/Volumes/ssd/Séries`
- Movie staging: `/Volumes/ssd/Films/.incoming/Movies Nerd`
- Series staging: `/Volumes/ssd/Séries/.incoming/Movies Nerd`

Create or mount the two library roots before the first transfer. Movies Nerd creates only the hidden staging subdirectory when a confirmed transfer is prepared.

## Verify the machine

From the installed skill directory, run:

```sh
python3 scripts/check_environment.py
scripts/run_sandboxed.sh check-environment
python3 scripts/qbittorrent_api.py status
```

The checker is read-only. A failed qBittorrent status usually means the application is closed, the Web UI is disabled, the port differs, or the environment credentials are missing.
