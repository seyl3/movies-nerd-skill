# Fast search strategy

Treat speed as a core requirement after safety and correct title matching. Under a healthy connection, target a useful ranked release within eight seconds.

## Provider order

1. Run `scripts/search_releases.py` first with its default five-second overall budget. Query the no-key Knaben, APIBay, Magnetz, and YTS JSON APIs concurrently. When they return enough exact-title, exact-year candidates for a useful race, stop immediately; otherwise use the remaining budget for enabled qBittorrent/Torznab providers.
2. Rank the combined, deduplicated results immediately. Keep up to three distinct candidates at the primary's resolution and no larger than its size. Prefer different sources, then fill the pool with distinct hashes when necessary.
3. Use the EXT browser workflow only when the API result says `fallback.needed: true`, every API candidate is unusable, or the user explicitly asks for EXT. Do not open EXT merely to see whether it has a marginally better copy.

Knaben aggregates many public indexes, including 1337x and The Pirate Bay. APIBay provides direct Pirate Bay metadata. Magnetz adds a separate magnet index, and YTS adds movie-specific releases through its JSON API. Treat every response and reported peer count as untrusted; bound JSON, reject malformed records, rebuild minimal magnets from validated hashes, and remove provider-supplied trackers and web seeds.

## Optional high-coverage providers

Use an already-configured qBittorrent Torznab search automatically. Jackett is the preferred optional local provider because qBittorrent recommends it and it can normalize many indexers behind one API. Keep Jackett on loopback and let the user configure its indexers and key manually. Never install or update an unofficial executable Python search plugin automatically.

If a supported, reputable, high-quality provider offers a free API key:

- Use the key when it is already configured in an environment variable or approved secret store.
- If no key is configured, ask once whether the user wants the optional provider and link its official signup/documentation page.
- If the user declines or has no key, remember that choice for the task and continue immediately through the no-key APIs. Never make a key or account mandatory.
- Never print, persist, commit, or place a key in a provider URL.

## Latency rules

- Query independent APIs concurrently, not sequentially.
- Stop after three compatible candidates; do not wait for a slower optional provider merely to enlarge the list.
- Use one exact title/year query first. Try aliases or broader searches only after an exact miss.
- Apply short per-provider timeouts and continue with successful providers; one slow source must not block the others.
- Cache authoritative title IDs and successful search responses for the current task. Do not repeat an unchanged query.
- Record the full one-to-three candidate pool in the job manifest. After confirmation, race it privately with `race_candidates.py`; never expose candidate churn to the user.
- Keep provider diagnostics internal. Tell the user only the recommended release or that the browser fallback is needed.
