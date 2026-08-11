# Resumable job state

Create one job manifest with `scripts/job_manifest.py` immediately after resolving the title and year. Keep it under the selected staging root and reuse it until import succeeds.

- Record the authoritative IDs, ranked release, confirmed source and size, qBittorrent hash, destination plan, backup release, prepared artifacts, cached provider results, full media probe, and step statuses.
- Update the manifest after each material transition: confirmation, Gate 1, transfer start/completion, Gate 2, subtitle coverage, and import.
- Resume from completed steps instead of rescanning the library, repeating a provider lookup, or asking the same question again.
- Never store passwords, API keys, tokens, cookies, authorization headers, or browser data. The helper rejects credential-like fields, bounds content, writes atomically with user-only permissions, and redacts magnets and torrent hashes from ordinary output.
- Treat a missing, malformed, symlinked, out-of-staging, or identity-mismatched manifest as invalid. Reconstruct safe state from authoritative local sources; do not trust or execute its contents.
- Remove the staging job only after successful import or explicit user-approved cleanup. It is operational state, not library metadata, and must never be copied into the final movie or series folder.
