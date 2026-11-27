"""Proxy engine: parsing, protocol auto-detection, health tracking, rotation.

A proxy failure is treated as a *transport* event, never as a website result.
`ProxyPool.pick()` hands out proxies; the HTTP layer reports outcomes back via
`report()` so the pool can track health, latency, quarantine and recovery.
"""

from __future__ import annotations

import asyncio
import itertools
import random
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

SOCKS_SCHEMES = ("socks5", "socks5h", "socks4", "socks4a")
HTTP_SCHEMES = ("http", "https")


@dataclass
class ProxySpec:
    scheme: Optional[str]  # http | socks4 | socks4a | socks5 | None (unknown)
    host: str
    port: int
    username: Optional[str] = None
    password: Optional[str] = None
    raw: str = ""

    @property
    def key(self) -> str:
        auth = f"{self.username}:{self.password}@" if self.username else ""
        scheme = self.scheme or "detect"
        return f"{scheme}://{auth}{self.host}:{self.port}"

    @property
    def display(self) -> str:
        auth = f"{self.username}:***@" if self.username else ""
        scheme = self.scheme or "detect"
        return f"{scheme}://{auth}{self.host}:{self.port}"

    @property
    def is_socks(self) -> bool:
        return self.scheme in SOCKS_SCHEMES

    @property
    def is_http(self) -> bool:
        return self.scheme in HTTP_SCHEMES

    def with_scheme(self, scheme: str) -> "ProxySpec":
        return ProxySpec(scheme, self.host, self.port, self.username, self.password, self.raw)


def parse_proxy_line(line: str) -> Optional[ProxySpec]:
    """Parse a proxy from common textual formats:

    scheme://[user:pass@]host:port
    [user:pass@]host:port
    host:port:user:pass
    host:port user:pass
    """
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    scheme: Optional[str] = None
    rest = line
    if "://" in line:
        scheme, rest = line.split("://", 1)
        scheme = scheme.lower()
        if scheme == "socks":
            scheme = "socks5"
        if scheme not in SOCKS_SCHEMES and scheme not in HTTP_SCHEMES:
            scheme = None
    username = password = None
    if "@" in rest:
        auth, rest = rest.rsplit("@", 1)
        if ":" in auth:
            username, password = auth.split(":", 1)
        else:
            username = auth
    parts = rest.replace(" ", ":").split(":")
    if len(parts) == 4:
        host, port, username, password = parts[0], parts[1], parts[2], parts[3]
    elif len(parts) == 2:
        host, port = parts
    else:
        return None
    try:
        port_i = int(port)
    except ValueError:
        return None
    if not (0 < port_i < 65536) or not host:
        return None
    return ProxySpec(scheme, host, port_i, username, password, line)


# ---------------------------------------------------------------------------
# protocol auto-detection (used when the line had no scheme)


async def detect_protocol(host: str, port: int, timeout: float = 8.0) -> Optional[str]:
    """Detect whether host:port speaks SOCKS5, SOCKS4 or HTTP proxy.

    Each probe uses its own connection so a failed probe cannot poison the
    next one.
    """

    async def probe(payload: bytes, expect_n: int) -> Optional[bytes]:
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout
            )
        except Exception:
            return None
        try:
            writer.write(payload)
            await writer.drain()
            return await asyncio.wait_for(reader.read(expect_n), timeout=6.0)
        except Exception:
            return None
        finally:
            try:
                writer.close()
            except Exception:
                pass

    # HTTP proxy: an absolute-form GET returns an HTTP status line
    data = await probe(
        b"GET http://example.com/ HTTP/1.0\r\nHost: example.com\r\n\r\n", 16
    )
    if data and data[:5].upper().startswith(b"HTTP/"):
        return "http"
    # SOCKS5 greeting: version 5, one method, no-auth (0x00)
    data = await probe(b"\x05\x01\x00", 2)
    if data and data[:1] == b"\x05":
        return "socks5"
    # SOCKS4: a malformed benign connect still answers with a 0x00 reply byte
    data = await probe(b"\x04\x01\x00\x00\x00\x00\x00\x00\x00", 8)
    if data and data[:1] == b"\x00":
        return "socks4"
    return None


# ---------------------------------------------------------------------------
# pool


@dataclass
class ProxyStats:
    key: str
    requests: int = 0
    successes: int = 0
    proxy_failures: int = 0
    consecutive_failures: int = 0
    avg_latency: float = 0.0
    last_latency: float = 0.0
    quarantined_until: float = 0.0
    inflight: int = 0
    status: str = "unknown"  # unknown | healthy | dead | quarantined

    @property
    def healthy(self) -> bool:
        return time.monotonic() >= self.quarantined_until and self.status != "dead"


@dataclass
class PoolSnapshot:
    total: int = 0
    healthy: int = 0
    quarantined: int = 0
    dead: int = 0
    inflight: int = 0
    proxy_failures: int = 0


class ProxyPool:
    """Round-robin / random / health-aware proxy selection with per-proxy
    concurrency limits, quarantine and automatic recovery."""

    def __init__(
        self,
        specs: List[ProxySpec],
        health_threshold: int = 3,
        quarantine_seconds: float = 120.0,
        detector: Optional[callable] = None,
        detect_timeout: float = 8.0,
    ):
        self.health_threshold = health_threshold
        self.quarantine_seconds = quarantine_seconds
        self._detector = detector or detect_protocol
        self._detect_timeout = detect_timeout
        self._stats: Dict[str, ProxyStats] = {}
        self._by_key: Dict[str, ProxySpec] = {}
        self._order: List[str] = []
        self._rr = itertools.count()
        self._lock = asyncio.Lock()
        for spec in specs:
            self._stats[spec.key] = ProxyStats(spec.key)
            self._by_key[spec.key] = spec
            self._order.append(spec.key)
        self.total_requests = 0
        self.total_proxy_failures = 0
        self._detected = False
        self._sems: Dict[str, asyncio.Semaphore] = {}
        self._per_proxy_limit = 10

    # ---- construction helpers ----

    @classmethod
    def from_file(cls, path: str, **kwargs) -> "ProxyPool":
        specs: List[ProxySpec] = []
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                spec = parse_proxy_line(line)
                if spec:
                    specs.append(spec)
        return cls(specs, **kwargs)

    async def resolve_schemes(self) -> None:
        """Auto-detect protocols for entries lacking a scheme."""
        to_detect = [s for s in self._by_key.values() if s.scheme is None]
        if not to_detect:
            self._detected = True
            return
        sem = asyncio.Semaphore(64)

        async def one(spec: ProxySpec) -> None:
            async with sem:
                scheme = await self._detector(spec.host, spec.port, self._detect_timeout)
            if scheme:
                new = spec.with_scheme(scheme)
                del self._stats[spec.key]
                del self._by_key[spec.key]
                self._stats[new.key] = ProxyStats(new.key)
                self._by_key[new.key] = new
            else:
                self._stats[spec.key].status = "dead"

        await asyncio.gather(*(one(s) for s in to_detect))
        self._order = list(self._by_key.keys())
        self._detected = True

    # ---- selection ----

    async def pick(
        self, exclude: Optional[List[str]] = None, strategy: str = "health"
    ) -> Optional[ProxySpec]:
        """Return the next proxy to try. Health-aware strategy prefers
        proxies with no recent failures; falls back to any non-dead proxy;
        returns None if everything is quarantined/dead."""
        async with self._lock:
            now = time.monotonic()
            exclude = set(exclude or [])
            candidates = [
                k
                for k in self._order
                if k not in exclude
                and now >= self._stats[k].quarantined_until
                and self._stats[k].status != "dead"
            ]
            if not candidates:
                return None
            if strategy == "random":
                key = random.choice(candidates)
                return self._by_key[key]
            if strategy == "round_robin":
                for _ in range(len(candidates) * 2):
                    key = self._order[next(self._rr) % len(self._order)]
                    if key in candidates:
                        return self._by_key[key]
                return self._by_key[candidates[0]]
            # health-aware: least inflight, then fewest consecutive failures,
            # then best latency, then round-robin among ties
            def rank(k: str) -> Tuple[int, int, float, int]:
                s = self._stats[k]
                return (
                    1 if s.status == "healthy" else 0,
                    -s.inflight,
                    -s.consecutive_failures,
                    -int(s.avg_latency * 1000),
                )

            candidates.sort(key=rank, reverse=True)
            best = candidates[0]
            top = rank(best)
            tied = [k for k in candidates if rank(k) == top]
            key = tied[next(self._rr) % len(tied)]
            return self._by_key[key]

    def set_per_proxy_limit(self, limit: int) -> None:
        self._per_proxy_limit = limit
        for key in self._order:
            self._sems[key] = asyncio.Semaphore(limit)

    def sem_for(self, spec: ProxySpec) -> asyncio.Semaphore:
        sem = self._sems.get(spec.key)
        if sem is None:
            sem = asyncio.Semaphore(self._per_proxy_limit)
            self._sems[spec.key] = sem
        return sem

    # ---- reporting ----

    def report_request_start(self, spec: ProxySpec) -> None:
        self._stats[spec.key].inflight += 1
        self.total_requests += 1

    def report_success(self, spec: ProxySpec, latency: float) -> None:
        s = self._stats[spec.key]
        s.inflight = max(0, s.inflight - 1)
        s.requests += 1
        s.successes += 1
        s.consecutive_failures = 0
        s.last_latency = latency
        s.avg_latency = (
            s.avg_latency * (s.successes - 1) + latency
        ) / s.successes if s.successes else latency
        s.status = "healthy"

    def report_proxy_failure(self, spec: ProxySpec, kind: str) -> None:
        """Record a proxy-side failure and quarantine if it keeps failing."""
        s = self._stats[spec.key]
        s.inflight = max(0, s.inflight - 1)
        s.requests += 1
        s.proxy_failures += 1
        s.consecutive_failures += 1
        self.total_proxy_failures += 1
        if kind == "auth_rejected":
            s.status = "dead"
            return
        if s.consecutive_failures >= self.health_threshold:
            s.quarantined_until = time.monotonic() + self.quarantine_seconds
            s.status = "quarantined"

    def report_ambiguous(self, spec: ProxySpec) -> None:
        """Neither a clean success nor a proven proxy fault (e.g. connection
        reset mid-response): counts against health but is retryable."""
        s = self._stats[spec.key]
        s.inflight = max(0, s.inflight - 1)
        s.requests += 1
        s.consecutive_failures += 1
        if s.consecutive_failures >= self.health_threshold * 2:
            s.quarantined_until = time.monotonic() + self.quarantine_seconds // 2
            s.status = "quarantined"

    def report_destination_response(self, spec: ProxySpec, latency: float) -> None:
        """We got a genuine destination HTTP response: the proxy works."""
        self.report_success(spec, latency)

    def healthy_count(self) -> int:
        now = time.monotonic()
        return sum(
            1 for s in self._stats.values() if now >= s.quarantined_until and s.status != "dead"
        )

    def recoverable_count(self) -> int:
        return sum(1 for s in self._stats.values() if s.status != "dead")

    def snapshot(self) -> PoolSnapshot:
        now = time.monotonic()
        snap = PoolSnapshot(total=len(self._order))
        for s in self._stats.values():
            if s.status == "dead":
                snap.dead += 1
            elif now < s.quarantined_until:
                snap.quarantined += 1
            else:
                snap.healthy += 1
            snap.inflight += s.inflight
        snap.proxy_failures = self.total_proxy_failures
        return snap

    @property
    def has_proxies(self) -> bool:
        return bool(self._order)

    def display_list(self) -> List[str]:
        return [self._by_key[k].display for k in self._order]
