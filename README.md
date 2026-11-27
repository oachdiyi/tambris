# SiteCheck Pro

High-performance, asynchronous **website & proxy checker** with accurate
classification, a reliable proxy engine, instant session resume, and a
polished interactive console. Python 3.9+, Windows and Linux.

## Features

- **Simple mode** — maximum speed: HTTP status-based classification, HEAD
  first with controlled GET fallback, no body classification, minimal
  requests.
- **Advanced mode** — accurate validation: title, visible text, headers,
  final URL, contextual fingerprint matching with confidence scoring, soft-404
  and parked-domain detection, JS-heavy site support, and an UNKNOWN category
  when evidence is insufficient.
- **Proxy engine** — HTTP, HTTPS, SOCKS4, SOCKS4a, SOCKS5, authenticated HTTP
  and SOCKS5, automatic protocol detection, health tracking, latency stats,
  round-robin / random / health-aware rotation, per-proxy concurrency limits,
  quarantine & recovery. A proxy failure is **never** a website result.
- **Sessions** — SQLite checkpoints with byte-offset input position, persisted
  dedup and results, instant resume (never recounts the input file), safe
  recovery after crashes.
- **Streaming input** — line-by-line reading with incremental deduplication;
  tens of millions of URLs never need to fit in RAM.
- **Live dashboard** — flicker-free (only changed lines are rewritten), CPM /
  avg / peak, processed / remaining, per-category counts, proxy failures,
  workers, elapsed, ETA, resize handling, pause & stop keys.

## Installation

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## Usage

```bash
python -m sitecheck
```

Main menu:

1. **Start New Session** — pick the URL file, mode, optional proxy file, and a
   performance preset (Low / Balanced / High / Extreme / Custom).
2. **Resume Previous Session** — continues from the last checkpoint byte
   offset without re-reading processed data; asks whether to reuse the
   previous proxy file and lets you change the performance preset.
3. **Settings** — edit every runtime parameter.
4. **Exit** — terminal state restored.

During a run: `P` pauses/resumes, `Q` stops and saves (safe to resume later).

### Input format

One URL per line (`examples/urls.txt`); `http://`/`https://` optional (bare
hosts get HTTPS), blank lines and `#` comments ignored.

### Proxy format

One proxy per line (`examples/proxies.txt`):
`scheme://[user:pass@]host:port`, `host:port`, or `host:port:user:pass`.
Scheme is auto-detected (SOCKS5 / SOCKS4 / HTTP) when omitted.

## Output files

Written to `output/` (configurable), tab-separated with a header:
`url | final_url | status_code | category | reason | proxy | ts`

| File                 | Meaning                                                        |
|----------------------|----------------------------------------------------------------|
| `success.txt`        | Simple: `VALID` (2xx). Advanced: `SUCCESS`.                    |
| `dead.txt`           | 404/410, permanent NXDOMAIN, confirmed soft-404s, suspended.   |
| `restricted.txt`     | 401, 403, 405, 429, 451.                                       |
| `parked.txt`         | Parked domains and marketplace pages (advanced mode).          |
| `failed.txt`         | Advanced: FAILED (broken/unconfigured, CDN & server errors).   |
| `errors.txt`         | Simple: `ERROR` (400, 5xx, timeouts, TLS, resets).             |
| `unknown.txt`        | Advanced: insufficient evidence — never a guess.               |
| `proxy_failures.txt` | Proxy-side failures and deferred URLs. **Not** website results.|

Rules: successful redirect → final URL saved; no redirect → original URL;
failed/unresolved redirects → error/unknown files; intermediate redirect URLs
are never saved as results; a missing page (`/donate`) never marks the whole
domain dead.

## Status classification (Simple mode)

- **VALID** — 2xx (including redirect chains ending in 2xx).
- **DEAD** — 404, 410, permanent DNS NXDOMAIN.
- **RESTRICTED** — 401, 403, 405, 429, 451.
- **ERROR** — 400, other 4xx, 5xx, timeouts, TLS failures, resets, refused
  connections, redirect loops/chains, temporary DNS failures.

## Proxy failure contract

1. A URL is assigned to a proxy.
2. If the proxy cannot establish a connection (unreachable, auth rejected,
   CONNECT failure, proxy timeout) the proxy is marked unhealthy.
3. The **same URL** is retried on another working proxy until a reliable
   destination response is obtained or the retry limit is reached.
4. Only then is the website response classified.
5. Ambiguous outcomes (e.g. connection reset mid-response, suspected
   proxy-generated error pages) keep the URL unresolved and retryable.
6. With no working proxies left, URLs are paused/deferred — never marked
   DEAD, FAILED, or VALID — and no silent direct fallback ever happens.

## Sessions & resume

Sessions live in `sessions/*.db` (SQLite, WAL). Checkpoints commit the exact
input byte offset, dedup state, pending queue, all results, and settings. On
resume the app re-queues only unprocessed URLs, never recounts the input file,
preserves output files and counters, and tolerates preset changes.

## Performance

- Fully asynchronous (aiohttp), pooled sessions, connection reuse, cached TLS
  contexts, compiled fingerprint cache.
- Streaming file reader with bounded memory and incremental SQLite-backed
  dedup.
- Controlled local benchmark (loopback, **not** real-network performance):
  see `benchmarks/benchmark.py`.

```text
$ python benchmarks/benchmark.py 20000 500
  urls: 20000   concurrency: 500   elapsed: 11.0s
  avg CPM ~ 108,000   peak CPM ~ 150,000   (local loopback)
```

Real-network CPM is dominated by latency and proxy quality and will be much
lower; accuracy is never sacrificed for CPM.

## Testing

```bash
pip install -r requirements.txt pytest pytest-asyncio
python -m pytest tests/ -v
```

The suite spins up local HTTP/HTTPS target servers, an HTTP forward proxy
(absolute-form + CONNECT + basic auth) and SOCKS4/SOCKS5 servers, and covers
every proxy protocol, auth, rotation, dead→working proxy failover,
proxy-vs-destination failure separation, CONNECT tunneling, DNS/TLS failures,
all relevant HTTP statuses, soft-404/parked/hosting placeholders, JS-heavy
sites, redirect chains & loops, final-URL saving, HEAD/GET inconsistencies,
large-file streaming & dedup, interruption & instant resume, and CPM math.

Note: Windows console behavior (VT enabling, `msvcrt` key input, terminal
restore) is implemented via guarded code paths and is exercised on POSIX by
the non-TTY fallback tests; it was not executed on a real Windows host while
packaging this release.

## Project layout

```
sitecheck/
  cli.py          interactive console and menus
  ui.py           colors, prompts, flicker-free dashboard
  config.py       settings & performance presets
  urlproc.py      URL normalization
  input_stream.py streaming reader, byte offsets, dedup
  session.py      SQLite session store & checkpoints
  proxies.py      proxy parsing, detection, pool, health, rotation
  http_client.py  async HTTP layer, failure taxonomy
  checker.py      retry contract, HEAD/GET strategy, TLS fallback
  classify.py     simple-mode classification
  advanced.py     advanced-mode classification & confidence
  fingerprints.py fingerprint database
  runner.py       loader, workers, checkpoints, deferral
  results.py      output writers
  stats.py        CPM / ETA statistics
tests/            automated test suite (local servers & proxies)
benchmarks/       controlled CPM benchmark
examples/         sample configuration, proxy list, URL list
```

## Changelog

See [CHANGELOG.md](CHANGELOG.md).
