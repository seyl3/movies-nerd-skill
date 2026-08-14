# Library policy

## Release preference

1. Prefer an efficient 2160p/4K release when the improvement is worthwhile, the complete payload is no larger than 15 GiB, it has healthy peers, and it is not CAM, telesync, screener, or an obvious upscale.
2. Otherwise prefer the best healthy, space-efficient 1080p release.
3. Prefer HEVC/x265 or AV1 for space-efficient 4K; accept AVC/x264 for 1080p.
4. Prefer the original-language audio, then lossless or 5.1 audio when size remains reasonable.
5. Use reported seed counts as discovery hints only. Prefer releases that have historically produced live swarms, then let the bounded qBittorrent probe measure actual health. Penalize ambiguous titles, missing years, password-protected archives, samples, and release spam.
6. Prefer a single main-feature payload. The user rarely wants extras; skip featurettes, trailers, interviews, deleted scenes, samples, and bonus discs unless explicitly requested.
7. Prefer MKV as the final media container. It is acceptable to download MP4, MOV, AVI, or another supported source container and remux it to MKV with `ffmpeg -c copy` after verification. Do not re-encode solely to obtain MKV.

### Size efficiency

- Compare size against authoritative runtime in GiB/hour; raw total size alone is misleading for unusually short or long films.
- The current collection baseline for 45 feature-length 1080p films is 1.001 GiB/hour median, 1.334 GiB/hour at the 75th percentile, and 2.045 GiB median total size.
- Use 1.35 GiB/hour as the normal 1080p target and 1.80 GiB/hour as a soft maximum. A 90-minute 5 GiB release is about 3.33 GiB/hour and should lose to a credible 2–2.5 GiB encode with comparable source, codec, audio, and completeness.
- For 2160p use a 2.75 GiB/hour target and 4.00 GiB/hour soft maximum; for 720p use 0.85 and 1.25 GiB/hour respectively. The 15 GiB total ceiling still applies.
- Treat these as ranking targets, not hard proof of quality. Grain-heavy restorations, HDR/Dolby Vision, 10-bit video, lossless or unusually rich audio, superior masters, and longer cuts can justify more space. Disclose the advantage and size cost before choosing the larger release.
- Flag an encode below 45% of the resolution target for source-quality review. Never optimize size by accepting visibly damaged video, missing scenes, incorrect frame rate, poor audio, or an upscale.

Use GiB internally (`1 GiB = 1,073,741,824 bytes`). Treat tracker-reported sizes as provisional until the torrent client reads metadata.

## Library roots

- Ask the user for separate Movies and Series roots before scanning or writing a library.
- Use `MOVIES_NERD_MOVIES_ROOT` and `MOVIES_NERD_SERIES_ROOT` as absolute paths for bundled scripts.
- If the user does not specify roots, default to `~/Documents/Movies` and `~/Documents/Series`.
- Keep the roots distinct and non-nested. Do not infer that they share a drive, volume, or parent directory.

## Movie layout

Root: `<MOVIES_ROOT>`

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

- Resolve titles by exact IMDb identity rather than assuming the user's wording is the original title.
- If the authoritative original title itself is English or French, use it alone: `The Great Escape (1963)` and `La Collectionneuse (1967)`. Do not classify a multilingual film from all of its spoken languages.
- For an original title in every other language, use the French title followed by the original title in parentheses: `Tout sur ma mère (Todo sobre mi madre) (1999)`. Do not add a duplicate parenthetical when both titles are identical.
- Use that resolved display title consistently for the folder, video, NFO, poster, and subtitle basename. Store the unmodified original title in the NFO `originaltitle` field.
- Use `[2160p]`, `[1080p]`, `[720p]`, `[576p]`, or `[480p]`; do not include codec, source, audio, release-group, or website spam in the final filename.
- Group by director. Use `Other` when that director has no other major movie in the collection or the title is their only major feature. When a second major movie by that director is added, create the director folder and move both films out of `Other`.
- Name the main poster exactly `<Title> (<Year>).png` for compatibility with the existing collection.

## Series layout

Root: `<SERIES_ROOT>`

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
- Prefer an existing verified embedded or sidecar track, then the OpenSubtitles API when `OPENSUBTITLES_API_KEY` is already configured, then the official no-key Stremio subtitle endpoint automatically.
- Never ask for a subtitle API key. If the user supplies one without prompting, use it through the environment and never echo it; otherwise remain on the no-key route.
- Search the no-key service by authoritative IMDb ID and exact series episode identity. Try a bounded set in provider order and keep the first English/French track that passes full SRT and runtime validation.
- Verify title, year, movie versus episode identity, season/episode numbers, release source, runtime, and timing coverage. A forced or partial track does not satisfy full-language coverage.
- Do not hard-code subtitle-provider API keys in the skill. Read credentials from `OPENSUBTITLES_API_KEY` or an approved secret store only when needed, and never echo or commit them.

## Validation and cleanliness

- Remove `.DS_Store`, AppleDouble `._*`, release URLs, advertisements, unrelated installer shortcuts, and Portuguese sidecar subtitles from completed Movies Nerd jobs after verifying the payload. Do not remove Portuguese audio tracks. Do not keep per-job quarantine after a successful or terminal cleanup.
- Preserve featurettes under `Featurettes/` and exclude them from main-feature naming rules.
- Validate all NFO files as XML and all images as decodable images before reporting completion.
