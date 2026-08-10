# Security audit of the inspiration skill

Audit date: 2026-08-10

## Scope

- MCP Market listing: `torrent-search-download`
- Publisher repository: `prateekmedia/agent-skills` at commit `db8f91079eefaf9789946cd941aa14b8ae0f024a`
- Helper repository referenced by the current skill: `prateekmedia/Borrow` at commit `8aff1bbff31b6194cd12ab021fe23fa29f19aee4`
- The older `theriturajps/ArcTorrent` repository named by the marketplace page was unavailable during the audit.

## Result

Do not install or execute the original skill as-is. No obvious credential theft, destructive filesystem command, dynamic evaluation, or direct malware behavior was found in the inspected source. The package is nevertheless operationally unsafe and internally inconsistent.

## Material findings

1. The marketplace instructions and repository disagree. The marketplace page directs the agent to clone and run ArcTorrent, while the repository's `borrow/SKILL.md` directs it to Borrow.
2. `.claude-plugin/marketplace.json` points at `./torr`, which does not exist; the actual directory is `borrow`.
3. The workflow clones third-party source, runs `npm install`, and starts a background Express scraper without a review or confirmation boundary.
4. The helper listens without an explicit loopback host and enables permissive CORS, exposing an unauthenticated scraping service more broadly than necessary.
5. `npm audit --package-lock-only --ignore-scripts` reported 11 known dependency vulnerabilities: 1 critical, 7 high, and 3 moderate. The affected chain included direct Axios and Express dependencies and a critical transitive `form-data` issue.
6. `borrow/scripts/download.js` imports `webtorrent`, but the skill contains no package manifest for that script. It also lacks the requested 15 GiB cap, staging boundary, payload-type validation, confirmation boundary, and sandbox.
7. The inspected code does not support EXT directly. Both `ext.to` and `search.extto.com` presented Cloudflare challenges during the audit, so a reliable unattended HTML scraper would require brittle anti-bot handling that Movies Nerd explicitly refuses to bypass.

## Remediation used here

Movies Nerd is a clean-room implementation and copies no executable code from the audited projects. It uses Python's standard library, qBittorrent's official loopback Web API, FFmpeg/ffprobe, fixed staging roots, dry-run defaults, explicit commit flags, payload validation, a 15 GiB cap, no delete endpoint, no background web server, and optional macOS sandbox profiles.

EXT support is limited to a fixed-host availability probe plus an interactive browser handoff. It reports Cloudflare or CAPTCHA challenges instead of bypassing them, and it never discovers random mirrors automatically.
