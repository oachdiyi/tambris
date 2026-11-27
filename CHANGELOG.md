# Changelog

## 1.0.0 - initial release

### Core
- Asynchronous checking engine (aiohttp) with pooled sessions, cached TLS
  contexts, per-proxy sessions for SOCKS, and connection reuse.
- Simple mode: status-only classification (VALID / DEAD / RESTRICTED / ERROR),
  HEAD-first with controlled GET fallback, redirect following
  (301/302/303/307/308), redirect loop and chain detection, HTTPS preference,
  HTTP fallback only for genuine TLS failures, certificate verification on.
- Advanced mode: title + visible-text + header inspection, contextual
  fingerprint matching with confidence scoring (60+ fingerprints: parked,
  marketplaces, hosting defaults, soft-404, CDN errors, browser error pages,
  suspension pages), JS-heavy site support, UNKNOWN when evidence is thin.

### Proxy engine
- HTTP/HTTPS/SOCKS4/SOCKS4a/SOCKS5, authenticated HTTP and SOCKS5.
- Automatic protocol detection for scheme-less entries.
- Health-aware, round-robin and random rotation; latency statistics.
- Per-proxy concurrency limits; quarantine with automatic recovery.
- Strict failure taxonomy: proxy failures never become website results;
  ambiguous outcomes stay unresolved and retryable; no silent direct fallback;
  separate proxy_failures.txt output.

### Sessions
- SQLite (WAL) session store; byte-offset checkpoints; persisted dedup and
  pending queue; instant resume without recounting; preset change on resume;
  proxy-file reuse prompt.

### Console
- Interactive menus with presets (Low / Balanced / High / Extreme / Custom).
- Flicker-free dashboard (changed-lines-only rendering), resize handling,
  pause/stop keys, Windows (PowerShell/CMD) VT support, terminal restore.

### Performance
- Streaming input with incremental dedup (bounded memory).
- Controlled loopback benchmark: ~109k avg CPM / ~150k peak CPM at
  concurrency 500 (20k URLs, local test server - not real-network).

### Testing
- 109 automated tests: unit + live-server integration for all proxy
  protocols, auth, rotation, failover, CONNECT tunneling, DNS/TLS errors,
  HTTP statuses, soft-404/parked/hosting/browser-error pages, JS-heavy sites,
  redirects/loops, final-URL saving, HEAD/GET inconsistencies, streaming,
  dedup, interruption/instant resume, and CPM math.
