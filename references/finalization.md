# Fast finalization

Start independent sidecar work as soon as `acquire.py` emits `enrichment: start-now`. Run `finalization_queue.py start-all` and perform these tasks concurrently while the video downloads:

- Canonical destination plan and authoritative IDs
- NFO metadata
- Exact poster, fanart, and clear logo when available
- English and French subtitle lookup and staged validation
- For movies, verified direct Letterboxd/SensCritique links and two recommendations

Keep prepared artifacts under the matching `.incoming/Movies Nerd` job directory. Mark each task with `finalization_queue.py mark`; never install beside incomplete media. Do not probe incomplete video or write provisional stream data into final NFO files.

## After completion

1. Transition to finalizing and stop the winner.
2. Run Gate 2 once with `select_payload.py`. If separate junk exists, clean-extract and re-verify only selected media. A bad NFO, image, subtitle, executable, archive, or macOS sidecar must not reject a verified video.
3. Reuse the saved probe for subtitles, track labels, chapters, and remux decisions. Keep a compliant MKV unchanged; use `edit_mkv_headers.py` for safe header-only fixes; otherwise perform one verified stream-copy remux. Never re-encode only for naming or container preference.
4. Validate prepared SRT, image, and XML files, then atomically install media and trusted sidecars.
5. Verify the final folder has media, NFO, and artwork. Remove every exact Movies Nerd-owned active, comparison loser, legacy standby, and failed qBittorrent entry with its bundled exact-hash command.
6. Run `finish_staging.py --job <manifest> --staged <exact-job-path> --commit`. Completion requires no matching job manifest, lock, temporary `.torrent`, per-job trash, AppleDouble sidecar, transfer, clean directory, or `.incoming/Movies Nerd` residue. The provider-health cache is the only allowed job-independent persistent state.

Target less than one minute from transfer completion to a compliant library entry. If an optional film link is unavailable after bounded verification, mark that link unavailable rather than delaying import. Never send the ready message until cleanup passes.
