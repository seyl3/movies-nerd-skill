# Movies Nerd

Movies Nerd is an opinionated all-in-one Codex skill for nontechnical film fans. Ask for one title or a batch and it starts automatically, then stays with every job until the library entries are ready.

Version: **2.7.2**

**Currently recommended:** use **GPT-5.6 Sol** with **High** reasoning.

## What it does

- Searches YTS, Knaben, APIBay, Magnetz, configured Torznab providers, then EXT only as a last resort.
- Runs the complete workflow through one foreground `movies-nerd download ...` command; the model never has to pass a generated job file between scripts.
- Understands French request titles: it resolves the exact movie first, searches the strongest English/international or original French title, and retains bounded French/original aliases.
- Names English- and French-language films with their original title; other films use `French title (original title)`.
- Opens qBittorrent, compares several candidates briefly, keeps only the best one, deletes duplicate partial downloads, and replaces a later dead or slow transfer automatically.
- Keeps the movie job attached through download, finalization, cleanup, and the ready handoff instead of stopping after “downloading.”
- Starts every successfully found title in a batch concurrently by default—six requested films means six active title downloads.
- Emits only meaningful lifecycle events, without repetitive percentage updates or manual sleep/status loops.
- Prepares metadata, original artwork, and English/French subtitles through one internal controller instead of spending model turns on each sidecar.
- Fully organizes series into show/season folders with show and episode NFOs, show/season artwork, per-episode English/French subtitles, specials, and multi-episode files.
- Prefers efficient 4K up to 15 GiB or space-efficient 1080p, MKV, original audio, and no extras.
- Adds clean names, NFO metadata, posters, fanart, track labels, and English/French subtitles through a no-key, no-browser default provider.
- Salvages verified video when separate release junk appears.
- Executes final verification/import and removes completed or failed job state, staging, trash, AppleDouble clutter, and exact Movies Nerd-owned qBittorrent entries before reporting success.
- Builds one compact, shallow director/title inventory during the transfer so recommendations reflect the collection and exclude movies already owned without expensive model-driven scanning.
- Returns direct Letterboxd/SensCritique links and two movie recommendations.

Requires Python 3.11+, qBittorrent 5.x, and FFmpeg/ffprobe. MKVToolNix is optional. Scripts use Python's standard library; no Node.js, Docker, scraper server, or extra qBittorrent CLI is required.

Install the `movies-nerd` folder and ask Codex to use `$movies-nerd`. Movie and series roots are selected independently; `/Volumes/ssd/Films` is a valid movie root. See [SKILL.md](SKILL.md) for the workflow and [setup](references/setup.md) for a raw machine.

MIT licensed.

## Development

CI validates the skill and version metadata, compiles every Python file, and runs the domain-split test suite on Python 3.11-3.14 across Linux and macOS.
