# Fast finalization

Use the download window to prepare every independent sidecar so completion is mostly a safe, atomic import.

## While the transfer runs

Start these jobs concurrently after Gate 1 succeeds:

- Resolve the canonical destination names and authoritative IDs.
- Fetch the exact movie or series metadata, poster, fanart, and clear logo when available.
- Search for English and French subtitles using the confirmed release name. Download only into staging and validate there; never write beside an incomplete video.
- Resolve the two post-download recommendations and verify their direct Letterboxd and SensCritique pages for a movie.

Keep every prepared artifact in the matching hidden staging root. Do not inspect incomplete media, generate final NFO runtime or stream fields from provisional tracker data, or move anything into the library before Gate 2 passes.

## After the transfer completes

1. Stop the torrent and run Gate 2 once. Reuse its selected path, duration, dimensions, codec, and companion inventory for the rest of the task.
2. Probe the selected media once for complete stream and chapter metadata. Reuse that result for subtitle coverage, track labels, and remux decisions.
3. If the source is already MKV and its language tags, clean track titles, default/forced dispositions, streams, and chapters comply with the library policy, keep it unchanged. Do not rewrite a multi-gigabyte file merely to produce an identical MKV.
4. If the container or track metadata needs correction, use one verified `ffmpeg -c copy` remux. Never re-encode solely for naming or container preference.
5. Validate the already-prepared subtitles, images, and XML, then atomically install the media and sidecars. Do not fetch the same metadata or artwork twice.

Do not rescan the entire library during finalization. Cache exact-title metadata, artwork URLs, subtitle candidates, probes, and recommendation lookups for the current task. Invalidate a cached result only when the confirmed release, authoritative ID, or destination changes.

## Time budget and user experience

- For a compliant MKV with sidecars prepared during download, target under one minute from transfer completion to a ready library entry.
- Run independent network work concurrently with bounded timeouts. A slow optional clear logo or recommendation link must not hold the media import open.
- Safety gates, exact-title matching, subtitle validation, and atomic installation are never skipped to meet a time target.
- Report one short completion message. Mention an optional artifact only when it remains unavailable; do not expose background job or cache details.
