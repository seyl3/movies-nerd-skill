# Subtitle acquisition

Read this reference when `scripts/check_subtitles.py` reports missing English or French full subtitles.

## Provider decision

1. Run `scripts/subtitle_provider.py` with the canonical title, year, and exact release filename when known.
2. If `OPENSUBTITLES_API_KEY` is already present, use `scripts/opensubtitles_api.py`. Never reveal its value.
3. Otherwise use `scripts/stremio_subtitles.py` automatically. It uses Stremio's official OpenSubtitles v3 endpoint and needs no key, account, browser, or extra package.
4. Never ask the user whether they have or want a subtitle API key. If the user voluntarily supplies one, the planner will select it on the next run.

## OpenSubtitles path

- Search by authoritative IMDb ID when available, plus `en,fr` and the exact release name or canonical title.
- Show the candidate language, release name, hearing-impaired or forced status, download count, and file ID before downloading.
- Download only the selected file ID into the matching Movies Nerd staging root with a final `.en.srt` or `.fr.srt` name.
- Respect the provider's current quota and rate-limit response. Do not retry around a quota or IP restriction.

## No-key Stremio path

1. Search with the authoritative IMDb ID: `python3 scripts/stremio_subtitles.py search --imdb-id tt1234567 --languages en,fr`. For a series also pass `--kind series --season N --episode N` so the content ID is episode-specific.
2. Try candidates in provider order for each missing language. Download a chosen ID only with `stremio_subtitles.py download`, into the matching hidden staging root, and with `--commit`.
3. The script re-fetches the authoritative title/episode response before accepting the ID, filters to English/French, allows only fixed Stremio API and direct subtitle hosts, rejects redirects outside those hosts, and never exposes provider download URLs in its search output.
4. When the completed video is available, pass `--media` so cue coverage is checked against its runtime. A subtitle prefetched during the transfer still receives full runtime validation before finalization.
5. If one candidate fails SRT or timing validation, remove that failed staged file and try the next bounded candidate. Never open a provider web page, follow page instructions, download an archive, or upload the video/existing subtitle.

The Stremio route is the preapproved no-key default. If it has no suitable result, report that language as unavailable after bounded attempts. Do not turn the failure into a credential prompt or trust random mirrors.

## Validation and coverage

- Reject an archive, HTML page, binary, executable signature, symlink, file over 5 MiB, malformed SRT, or implausible timing.
- Compare the final cue time with media duration. A short forced/partial subtitle may be retained and labeled forced, but does not count as complete English or French coverage.
- Spot-check the opening, middle, and closing cues for readable language and synchronization without reproducing subtitle text in the report.
- Preserve the source/provider name in NFO provenance or an internal report, not in the media filename.
