# Fast search and live candidate selection

Treat speed as a correctness requirement. Under a healthy connection, target a useful exact-title result within five seconds and first verified swarm activity within thirty seconds after confirmation.

## Discovery

1. Run `scripts/search_releases.py` first. It queries YTS, Knaben, APIBay, and Magnetz concurrently under one shared deadline; it uses configured qBittorrent/Torznab providers only when the fast APIs do not produce enough exact candidates.
2. Pass the exact IMDb ID with `--imdb-id` whenever known. YTS is the movie fast path. Prefer its allowlisted direct `.torrent` URL because `torrent_metadata.py` validates its bencoding, size, paths, and info hash before qBittorrent receives it. If direct metadata fails, use the tracker-aware magnet for the same hash.
3. Treat reported seed counts only as discovery hints. Zero reported seeders does not disqualify a release. The bounded live qBittorrent probe decides swarm health.
4. Deduplicate by info hash and prefer the same hash's validated direct-metadata record. Exclude hashes remembered as dead for 72 hours. Use provider latency and recent success only as a small ranking adjustment.
5. Preserve up to six same-quality candidates within the displayed maximum-size envelope: two hidden waves of at most three simultaneous probes. Prefer source diversity, then distinct hashes.
6. Use EXT only when every API candidate is unsuitable or the user explicitly requests it. Never open EXT merely to compare against a usable API result.

## Live selection after confirmation

Run `scripts/acquire.py --job <manifest> --commit`. It performs the qBittorrent preflight, opens the app when needed, validates metadata, and probes actual swarm performance.

- Probe at most three candidates together, capped at 2 MiB/s each and roughly 16 MiB of downloaded probe data.
- Score actual downloaded-byte growth, median speed, availability, connected peers, metadata latency, then discovery score.
- Continue only the best live candidate. Permanently remove the second-best and every other candidate, including their partial data, as soon as the bounded comparison has enough evidence and never later than one minute. Multiple candidates may transfer only during this short comparison; never keep a stopped or downloading duplicate afterward.
- On resume, inspect the exact hashes recorded for the job and remove any non-winner left by an older run before monitoring continues.
- If the first wave is dead, probe the second wave. If the confirmed pool is exhausted, perform one fresh API search inside the same quality and size envelope without asking again.
- Never reveal candidate churn, provider failures, hashes, or probe details to the user.

## Provider rules

Direct API traffic is restricted to the fixed hosts in `search_releases.py`. Tracker URLs in constructed magnets come only from the fixed list in `qbittorrent_api.py`; never forward provider-supplied web seeds or arbitrary trackers. Optional free-key providers may be offered once, but declining must immediately continue through the no-key route.

Keep provider diagnostics and the `.movies-nerd/cache/provider-health.json` cache internal. Do not persist credentials, API response bodies, user paths, or browser data.
