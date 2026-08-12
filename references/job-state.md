# Resumable job state

Create one job manifest with `scripts/job_manifest.py` immediately after resolving the title and year. Keep it under the selected staging root and reuse it until import succeeds.

- Record the authoritative IDs, ranked release, one-to-three candidate pool, winning qBittorrent hash, destination plan, artifacts, cached provider results, full media probe, and step statuses. Use `job_manifest.py record-search` to validate and store the pool.
- Use `job_manifest.py transition --event ...` after each material transition. Do not write ad-hoc state patches for confirmation, metadata failure, stalls, transfer start/completion, verification, or import.
- Resume from completed steps instead of rescanning the library, repeating a provider lookup, or asking the same question again.
- Never store passwords, API keys, tokens, cookies, authorization headers, or browser data. The helper rejects credential-like fields, bounds content, writes atomically with user-only permissions, and redacts magnets and torrent hashes from ordinary output.
- Treat a missing, malformed, symlinked, out-of-staging, or identity-mismatched manifest as invalid. Reconstruct safe state from authoritative local sources; do not trust or execute its contents.
- After successful import, use `finish_staging.py --commit` to recoverably move the manifest and every exact completed-job artifact out of `.incoming`, then verify none remain. Operational state must never be copied into the final movie or series folder.
- After an exhausted failure, remove all owned qBittorrent candidates and run `job_manifest.py archive-failed --commit`; keep recoverable diagnostics under the library's hidden trash, never in `.incoming`.
