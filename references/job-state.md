# Resumable v2 job state

Create one v2 manifest with `scripts/job_manifest.py` after resolving the title. It lives under `<LIBRARY>/.movies-nerd/jobs`, never inside `.incoming`, so a payload cleanup cannot destroy resumable control state.

- Record authoritative IDs, runtime, the displayed quality/size envelope, up to six confirmed candidates, the sole active hash, brief comparison outcomes, removed duplicates, attempts, dead candidates, prepared sidecars, media probes, destination, and step statuses. A legacy standby hash may be read only so it can be removed during resume; never create a new persistent standby.
- Resume from the manifest. `scripts/acquire.py` uses an atomic per-job lock and reconnects to an existing active torrent instead of repeating search or confirmation.
- Store no passwords, keys, tokens, cookies, authorization headers, browser data, raw API responses, or private inventory. Manifests are bounded, validated, atomic, and mode `0600`.
- Keep only the bounded cross-job provider-health cache after a job terminates. Completed manifests, failed manifests, locks, direct `.torrent` files, temporary artifacts, and per-job trash must not persist.
- On successful import, first remove every exact Movies Nerd-owned qBittorrent candidate with `qbittorrent_api.py remove --commit`. Then run `finish_staging.py --job ... --commit`. It refuses to delete the manifest until qBittorrent absence and final-library completeness are verified.
- On an exhausted terminal failure, remove and verify absence of every exact owned candidate before `job_manifest.py remove-failed --commit` removes the failed manifest. If cleanup cannot be verified, retain the manifest as resumable state; never claim cleanup succeeded.
