# Library policy

## Release preference

1. Prefer 2160p/4K when the complete payload is no larger than 15 GiB, has healthy peers, and is not CAM, telesync, screener, or an obvious upscale.
2. Otherwise prefer the best healthy 1080p release.
3. Prefer HEVC/x265 or AV1 for space-efficient 4K; accept AVC/x264 for 1080p.
4. Prefer the original-language audio, then lossless or 5.1 audio when size remains reasonable.
5. Prefer at least 10 seeders and strongly prefer more than 20. Penalize ambiguous titles, missing years, password-protected archives, samples, and release spam.
6. Prefer a single main-feature payload. The user rarely wants extras; skip featurettes, trailers, interviews, deleted scenes, samples, and bonus discs unless explicitly requested.
7. Prefer MKV as the final media container. It is acceptable to download MP4, MOV, AVI, or another supported source container and remux it to MKV with `ffmpeg -c copy` after verification. Do not re-encode solely to obtain MKV.

Use GiB internally (`1 GiB = 1,073,741,824 bytes`). Treat tracker-reported sizes as provisional until the torrent client reads metadata.

## Movie layout

Root: `/Volumes/ssd/Films`

```text
<Director>/
  <Title> (<Year>)/
    <Title> (<Year>) [<resolution>].mkv
    <Title> (<Year>) [<resolution>].nfo
    <Title> (<Year>) [<resolution>].en.srt
    <Title> (<Year>) [<resolution>].fr.srt
    <Title> (<Year>).png
    fanart.jpg
    clearlogo.png
```

- Preserve the authoritative display title and year.
- Use `[2160p]`, `[1080p]`, `[720p]`, `[576p]`, or `[480p]`; do not include codec, source, audio, release-group, or website spam in the final filename.
- Group by director. Use `Other` when that director has no other major movie in the collection or the title is their only major feature. When a second major movie by that director is added, create the director folder and move both films out of `Other`.
- Name the main poster exactly `<Title> (<Year>).png` for compatibility with the existing collection.

## Series layout

Root: `/Volumes/ssd/Séries`

```text
<Series> (<Year>)/
  tvshow.nfo
  poster.jpg
  fanart.jpg
  clearlogo.png
  seasonNN-poster.jpg
  Season NN/
    poster.jpg
    <Series> (<Year>) - SNNENN - <Episode Title> [<resolution>].mkv
    <Series> (<Year>) - SNNENN - <Episode Title> [<resolution>].nfo
    <Series> (<Year>) - SNNENN - <Episode Title> [<resolution>].jpg
```

- Use canonical aired-order season and episode numbers unless the user explicitly requests another order.
- Use official episode titles and premiere dates.

## Metadata and artwork

- Resolve exact IMDb/TMDB/TVmaze IDs before writing metadata.
- Movie NFO: title, original title, sort title, year, plot, premiered date, runtime, rating, genres, director, studio, country, and unique IDs.
- Show NFO: title, original title, year, plot, premiered date, status, runtime, rating, genres, studio/network, and unique IDs.
- Episode NFO: show title, episode title, season, episode, plot, aired date, runtime, rating, and unique ID.
- Prefer original-resolution TMDB artwork matched by exact external ID. Validate poster portrait orientation, fanart landscape orientation, and PNG alpha for clear logos.
- Do not substitute artwork from a similarly named title when an exact match is unavailable.

## Audio and subtitles

- Normalize embedded language tags to ISO 639-2 codes such as `eng`, `fre`, `ita`, and `spa`.
- Use clean titles such as `English`, `French`, and `English (SDH)`.
- Mark exactly one main audio track default.
- Clear default disposition from full subtitle tracks; preserve legitimate forced and hearing-impaired flags.
- Keep external English and French sidecars as `.en.srt` and `.fr.srt`. Do not retain Portuguese subtitles unless the user asks.
- Aim for both English and French coverage for every main movie and episode, using legitimate configured providers. Embedded tracks count when their language tags are verified.
- Do not hard-code subtitle-provider API keys in the skill. Read credentials from an approved environment variable or secret store only when needed.

## Integrity and cleanliness

- Recoverably quarantine `.DS_Store`, AppleDouble `._*`, release URLs, advertisements, unrelated installer shortcuts, and Portuguese sidecar subtitles after verifying the payload. Do not remove Portuguese audio tracks.
- Preserve featurettes under `Featurettes/` and exclude them from main-feature naming rules.
- Refresh `SHA256SUMS.txt` at the affected library root, excluding the manifest itself, staging, quarantine, and macOS sidecars.
- Validate all NFO files as XML, all images as decodable images, and dispersed checksum entries before reporting completion.
