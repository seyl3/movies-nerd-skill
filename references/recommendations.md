# Post-download recommendations

Use this after a movie—not a series episode—has been successfully imported into the library.

## Choose two films

Use the controller's `recommendation_context.owned_by_director` map. It is prepared during the transfer from one shallow folder scan and contains only director names plus `Title (Year)` labels. Do not run `find`, inspect video files, load every NFO, or rescan the library. The completed director is listed first.

1. Recommend one film genuinely similar to the completed movie. Base the match on tone, themes, movement, country, period, style, or audience affinity rather than title keywords alone.
2. Recommend one worthwhile different feature by the same director.
3. Exclude the completed title and every film present anywhere in `owned_by_director`.
4. If the director has no other meaningful feature, or every suitable one is already present, provide a second strong similar-film recommendation and label it `Another match`.
5. Keep each reason to one spoiler-free sentence. Do not turn the completion message into a long review or list.

Use repeated directors and the existing titles as a light taste signal when choosing between otherwise good candidates. Do not summarize, classify, or deeply analyze the whole collection; one pass over the compact map is enough.

Keep model and web work bounded: choose only the two final candidates, check them against the prepared map, and verify only their direct pages plus the completed film's pages. Never paste the inventory into the user-facing reply.

## Verify direct links

Film pages are current web lookups. Verify links for the completed movie and each recommended film:

1. Search Letterboxd and SensCritique using the exact title, year, and director.
2. Open the candidate page and verify its title, release year, and director.
3. Link to the canonical film page itself, never a search-results page.
4. Never guess or construct a slug from the title. If an exact page cannot be verified after a reasonable search, omit that platform's link and say `link unavailable` rather than linking the wrong film.
5. Browse read-only. Do not sign in, add watchlist entries, rate films, or publish anything.

## User-facing format

Keep the completion response short:

```text
Downloaded and organized: Movie (Year) [1080p]
Movie pages: Letterboxd · SensCritique

You may like:
- Similar — Film (Year): one brief reason. Letterboxd · SensCritique
- More from Director — Film (Year): one brief reason. Letterboxd · SensCritique

Have a good watch!
```

Make `Letterboxd` and `SensCritique` direct clickable links for the completed movie and each recommendation. Do not describe how the recommendations or links were found unless the user asks.
