# Fast finalization

`run_job.py` requests independent sidecar work as soon as a movie or series transfer starts. `finalization_queue.py start-all --job <manifest>` creates the exact per-job artifact directory and marks tasks `requested`, not `running`. Mark a task `running` only when work really begins and `complete` only with its validated artifact or bounded note.

Prepare these concurrently while the video downloads:

- Canonical destination plan and authoritative IDs
- NFO metadata
- Exact poster, plus fanart and clear logo when available
- English and French subtitle lookup and staged validation
- Verified direct Letterboxd/SensCritique links and two recommendations

Keep every prepared file under the exact `artifact_root` returned by the queue. Mark tasks with `finalization_queue.py mark --job ...`. The required `metadata`, `artwork`, `subtitle-en`, and `subtitle-fr` tasks carry regular files. Destination, film-link, and recommendation tasks may carry bounded notes. Metadata JSON includes the exact `title`, `year`, non-empty `directors`, authoritative `uniqueids`, `original_language`, direct `letterboxd_url`/`senscritique_url`, and up to two recommendation objects; use `stream_languages` only when a known stream needs an explicit index-to-language override. Optional `fanart.jpg` and `clearlogo.png` may sit beside the poster. Do not probe incomplete video or write provisional stream data into final NFO files.

## Foreground completion

Keep the same `run_job.py` process alive. Once the transfer and all required tasks are complete, it calls `finalize_job.py`, which stops the winner, runs Gate 2 once, clean-extracts verified media from separate junk, reuses the saved probe, performs only a verified stream-copy remux when needed, validates SRT/image/XML input, and atomically installs the entry. It then removes every exact owned candidate record and partial directory and runs strict staging cleanup. A recorded import transaction makes the final move resumable after interruption.

Never run the controller with shell background syntax. If the execution tool yields a session identifier, retain it and wait on that exact session; do not create shell sleeps or repeated status commands. The controller emits compact lifecycle events instead of periodic heartbeats and wakes for completion itself. Exit code 8 means preparation is incomplete: finish the already-requested work and resume the same job without asking the user again.

Completion requires no matching manifest, lock, temporary torrent, per-job trash, AppleDouble sidecar, transfer, clean directory, or Movies Nerd incoming residue. The provider-health cache is the only allowed job-independent persistent state. Target less than one minute from transfer completion to a compliant entry. A bounded unavailable optional link must not delay import, and the ready message must wait for clean completion.

## Series artifact contract

Series use the same queue and unified `run_job.py`, which dispatches to `finalize_series.py`. The metadata artifact is a JSON object with `show` and `episodes`. `show` contains title, year, plot and authoritative IDs. Every episode includes `source` (its exact downloaded relative path), `season`, `episode`, optional `episode_end`, title, plot, aired date, runtime, rating and unique IDs. Season `0` is Specials; `episode_end` creates a combined `SxxExx-Eyy` file.

For multiple episode files, each subtitle task is a JSON manifest containing `subtitles`, a list of `{source, path}` objects; paths are relative to the per-job artifact root. For one episode, a direct SRT remains valid. The artwork task may be a JSON manifest with relative `poster`, `fanart`, and `season_posters` paths, or a poster image accompanied by `fanart.jpg` and `seasonXX-poster.jpg`. Finalization writes `tvshow.nfo`, one episode NFO per media file, show and season artwork, and English/French sidecars only where embedded coverage is absent. It may merge new files into an existing show, but never overwrite an existing episode.
