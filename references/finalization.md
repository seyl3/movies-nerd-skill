# Fast finalization

`run_job.py` starts `prepare_artifacts.py` internally as soon as the transfer and artifact queue are ready. The model never orchestrates individual sidecars. The controller creates the exact per-job artifact directory, runs independent work concurrently, marks each task only when it truly starts or completes, and returns one compact result to `run_job.py`.

The controller prepares these concurrently while a movie downloads; episode-specific series work waits internally for the completed selected-file list:

- Canonical destination plan and authoritative IDs
- NFO metadata
- Exact poster, plus fanart and clear logo when available
- English and French subtitle lookup and staged validation
- Verified direct Letterboxd/SensCritique links and two recommendations

Keep every prepared file under the exact per-job artifact root. Metadata and artwork tasks carry regular files. Subtitle tasks carry validated SRT files or a bounded note only after completed-media inspection proves that language is embedded; series manifests may omit episodes with verified embedded coverage. Destination, film-link, and recommendation tasks carry bounded controller notes when optional data is unavailable. Metadata JSON includes the exact title/year, directors, authoritative IDs, stable available links, and stream-language overrides only when known. Optional fanart and clear-logo files sit beside the poster. Do not probe incomplete video or write provisional stream data into final NFO files.

The routine model path is exactly one `prepare_job.py` call followed by one attached `run_job.py` call. Do not inspect task state repeatedly, invoke provider scripts separately, parse their verbose candidate output, or call `finalization_queue.py mark`. Those actions waste tokens and can race the controller.

## Foreground completion

Keep the same `run_job.py` process alive. Once the transfer and all required tasks are complete, it calls `finalize_job.py`, which stops the winner, runs Gate 2 once, clean-extracts verified media from separate junk, reuses the saved probe, performs only a verified stream-copy remux when needed, validates SRT/image/XML input, and atomically installs the entry. It then removes every exact owned candidate record and partial directory and runs strict staging cleanup. A recorded import transaction makes the final move resumable after interruption.

Never run the controller with shell background syntax. If the execution tool yields a session identifier, retain it and wait on that exact session; do not create shell sleeps or repeated status commands. The controller emits compact lifecycle events instead of periodic heartbeats and wakes for completion itself. Exit code 8 means its bounded automated preparation needs resuming; run the same job again without asking the user or manually creating artifacts.

Completion requires no matching manifest, lock, temporary torrent, per-job trash, AppleDouble sidecar, transfer, clean directory, or Movies Nerd incoming residue. The provider-health cache is the only allowed job-independent persistent state. Target less than one minute from transfer completion to a compliant entry. A bounded unavailable optional link must not delay import, and the ready message must wait for clean completion.

## Series artifact contract

Series use the same queue and unified `run_job.py`, which dispatches to `finalize_series.py`. The metadata artifact is a JSON object with `show` and `episodes`. `show` contains title, year, plot and authoritative IDs. Every episode includes `source` (its exact downloaded relative path), `season`, `episode`, optional `episode_end`, title, plot, aired date, runtime, rating and unique IDs. Season `0` is Specials; `episode_end` creates a combined `SxxExx-Eyy` file.

For multiple episode files, each subtitle task is a JSON manifest containing `subtitles`, a list of `{source, path}` objects; paths are relative to the per-job artifact root. For one episode, a direct SRT remains valid. The artwork task may be a JSON manifest with relative `poster`, `fanart`, and `season_posters` paths, or a poster image accompanied by `fanart.jpg` and `seasonXX-poster.jpg`. Finalization writes `tvshow.nfo`, one episode NFO per media file, show and season artwork, and English/French sidecars only where embedded coverage is absent. It may merge new files into an existing show, but never overwrite an existing episode.
