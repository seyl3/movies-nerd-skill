# Fast search strategy

Treat speed as a core requirement after safety and correct title matching. Under a healthy connection, target a useful ranked release within eight seconds.

## Provider order

1. Run `scripts/search_releases.py` first with its default five-second overall budget. It queries the no-key Knaben and APIBay JSON APIs concurrently for up to two seconds. When they return at least three exact-title, exact-year, healthy candidates including a different-source backup, stop immediately; otherwise use the remaining budget for enabled qBittorrent/Torznab providers.
2. Rank the combined, deduplicated results immediately. Keep the best eligible release and one backup from a different source. Stop searching when the primary is healthy and the backup decision is made; do not download or add the backup.
3. Use the EXT browser workflow only when the API result says `fallback.needed: true`, every API candidate is unusable, or the user explicitly asks for EXT. Do not open EXT merely to see whether it has a marginally better copy.

Knaben aggregates many public indexes, including results originating from 1337x and The Pirate Bay. APIBay provides a second direct no-key path for Pirate Bay metadata. Treat both responses as untrusted; the helper bounds JSON, rejects malformed records, rebuilds minimal magnets from validated info hashes, and removes provider-supplied trackers and web seeds.

## Optional high-coverage providers

Use an already-configured qBittorrent Torznab search automatically. Jackett is the preferred optional local provider because qBittorrent recommends it and it can normalize many indexers behind one API. Keep Jackett on loopback and let the user configure its indexers and key manually. Never install or update an unofficial executable Python search plugin automatically.

If a supported, reputable, high-quality provider offers a free API key:

- Use the key when it is already configured in an environment variable or approved secret store.
- If no key is configured, ask once whether the user wants the optional provider and link its official signup/documentation page.
- If the user declines or has no key, remember that choice for the task and continue immediately through the no-key APIs. Never make a key or account mandatory.
- Never print, persist, commit, or place a key in a provider URL.

## Latency rules

- Query independent APIs concurrently, not sequentially.
- Stop after three exact, healthy candidates; do not wait for a slower optional provider merely to enlarge the list.
- Use one exact title/year query first. Try aliases or broader searches only after an exact miss.
- Apply short per-provider timeouts and continue with successful providers; one slow source must not block the others.
- Cache authoritative title IDs and successful search responses for the current task. Do not repeat an unchanged query.
- Record the primary and different-source backup in the staging job manifest. Keep the backup unadded; it exists only to avoid a second search after a stall.
- Keep provider diagnostics internal. Tell the user only the recommended release or that the browser fallback is needed.
