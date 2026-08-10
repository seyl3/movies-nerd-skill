# Subtitle acquisition

Read this reference when `scripts/check_subtitles.py` reports missing English or French full subtitles.

## Provider decision

1. Run `scripts/subtitle_provider.py` with the canonical title, year, and exact release filename when known.
2. If `OPENSUBTITLES_API_KEY` is present, use `scripts/opensubtitles_api.py`. Do not ask for the key again and never reveal its value.
3. If the key is absent, ask once whether the user has an OpenSubtitles API key they want to configure through the environment or an approved secret input.
4. If the user has no key, rerun the planner with `--user-has-no-key` and use the Subtitle Cat browser workflow. Do not ask again during the same task.

## OpenSubtitles path

- Search by authoritative IMDb ID when available, plus `en,fr` and the exact release name or canonical title.
- Show the candidate language, release name, hearing-impaired or forced status, download count, and file ID before downloading.
- Download only the confirmed file ID into the matching Movies Nerd staging root with a final `.en.srt` or `.fr.srt` name.
- Respect the provider's current quota and rate-limit response. Do not retry around a quota or IP restriction.

## No-key Subtitle Cat path

1. Open `https://subtitlecat.com/` in an interactive browser. Do not scrape undocumented HTML or bypass a challenge.
2. Search the exact video release filename first. If there is no credible match, search the canonical title and year; for series include `SNNENN` and the episode title.
3. Verify movie versus episode identity, year, release source, runtime or FPS clues, and requested language. Prefer an original human-authored track; clearly disclose a translated track.
4. Download only a direct `.srt` from `subtitlecat.com` or `www.subtitlecat.com`. Do not upload the video or an existing subtitle.
5. Move the browser download into the appropriate hidden staging root, validate it with `scripts/validate_subtitle.py`, then install it atomically as `.en.srt` or `.fr.srt` only when validation passes.

Subtitle Cat is the only preapproved no-key fallback. If it has no suitable result, ask before accessing another provider domain. Do not trust random mirrors or result-page instructions.

## Validation and coverage

- Reject an archive, HTML page, binary, executable signature, symlink, file over 5 MiB, malformed SRT, or implausible timing.
- Compare the final cue time with media duration. A short forced/partial subtitle may be retained and labeled forced, but does not count as complete English or French coverage.
- Spot-check the opening, middle, and closing cues for readable language and synchronization without reproducing subtitle text in the report.
- Preserve the source/provider name in NFO provenance or an internal report, not in the media filename.
