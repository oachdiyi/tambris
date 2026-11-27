"""Proxy engine tests: parsing, detection, health, rotation, quarantine."""

from __future__ import annotations

import asyncio
import time

import pytest

from sitecheck.proxies import ProxyPool, ProxySpec, parse_proxy_line


# ------------------------------------------------------------- parsing

def test_parse_scheme_host_port():
    spec = parse_proxy_line("http://1.2.3.4:8080")
    assert spec.scheme == "http" and spec.host == "1.2.3.4" and spec.port == 8080


def test_parse_socks5():
    spec = parse_proxy_line("socks5://1.2.3.4:1080")
    assert spec.scheme == "socks5" and spec.is_socks


def test_parse_bare_host_port():
    spec = parse_proxy_line("1.2.3.4:8080")
    assert spec.scheme is None and spec.host == "1.2.3.4"


def test_parse_auth():
    spec = parse_proxy_line("http://user:pass@1.2.3.4:8080")
    assert spec.username == "user" and spec.password == "pass"


def test_parse_host_port_user_pass():
    spec = parse_proxy_line("1.2.3.4:8080:user:pass")
    assert (spec.username, spec.password) == ("user", "pass")


def test_parse_socks_shorthand():
    assert parse_proxy_line("socks://1.2.3.4:1080").scheme == "socks5"


def test_parse_comments_and_garbage():
    assert parse_proxy_line("# comment") is None
    assert parse_proxy_line("") is None
    assert parse_proxy_line("nohost") is None
    assert parse_proxy_line("host:notaport") is None


# ------------------------------------------------------------ detection

async def test_detect_http_protocol(http_proxy):
    from sitecheck.proxies import detect_protocol

    scheme = await detect_protocol("127.0.0.1", http_proxy.port, timeout=5)
    assert scheme == "http"


async def test_detect_socks5_protocol(socks5_proxy):
    from sitecheck.proxies import detect_protocol

    scheme = await detect_protocol("127.0.0.1", socks5_proxy.port, timeout=5)
    assert scheme == "socks5"


async def test_pool_resolve_schemes(http_proxy, socks5_proxy):
    pool = ProxyPool(
        [
            ProxySpec(None, "127.0.0.1", http_proxy.port, raw="a"),
            ProxySpec(None, "127.0.0.1", socks5_proxy.port, raw="b"),
        ],
        detect_timeout=5,
    )
    await pool.resolve_schemes()
    schemes = sorted(s.scheme for s in pool._by_key.values())
    assert schemes == ["http", "socks5"]


# ---------------------------------------------------------------- health

def _pool(n=2, **kw) -> ProxyPool:
    return ProxyPool(
        [ProxySpec("http", f"10.0.0.{i}", 8080) for i in range(n)], **kw
    )


async def test_pick_returns_proxies():
    pool = _pool(3)
    picked = [await pool.pick() for _ in range(3)]
    assert all(p is not None for p in picked)
    assert len({p.key for p in picked}) == 3


async def test_pick_excludes():
    pool = _pool(2)
    first = await pool.pick()
    second = await pool.pick(exclude=[first.key])
    assert second.key != first.key


async def test_dead_proxy_skipped():
    pool = _pool(2)
    pool._stats[pool._order[0]].status = "dead"
    for _ in range(10):
        p = await pool.pick()
        assert p.key == pool._order[1]


async def test_quarantine_and_recovery():
    pool = _pool(1, health_threshold=2, quarantine_seconds=0.2)
    spec = pool._by_key[pool._order[0]]
    pool.report_request_start(spec)
    pool.report_proxy_failure(spec, "connect")
    pool.report_request_start(spec)
    pool.report_proxy_failure(spec, "connect")
    assert pool.healthy_count() == 0
    assert (await pool.pick()) is None
    await asyncio.sleep(0.25)
    assert pool.healthy_count() == 1
    assert (await pool.pick()) is not None


async def test_auth_rejection_marks_dead():
    pool = _pool(1)
    spec = pool._by_key[pool._order[0]]
    pool.report_proxy_failure(spec, "auth_rejected")
    assert pool.healthy_count() == 0


async def test_success_resets_health():
    pool = _pool(1, health_threshold=1, quarantine_seconds=60)
    spec = pool._by_key[pool._order[0]]
    pool.report_proxy_failure(spec, "connect")
    assert pool.healthy_count() == 0
    assert (await pool.pick()) is None
    # recovery time passes
    pool._stats[spec.key].quarantined_until = 0.0
    pool.report_request_start(spec)
    pool.report_success(spec, 0.05)
    assert pool.healthy_count() == 1


async def test_health_aware_prefers_healthy():
    pool = _pool(2)
    bad = pool._by_key[pool._order[0]]
    good = pool._by_key[pool._order[1]]
    pool._stats[bad.key].status = "quarantined"
    pool._stats[bad.key].quarantined_until = time.monotonic() + 60
    for _ in range(5):
        assert (await pool.pick()).key == good.key


def test_snapshot_counts():
    pool = _pool(3)
    pool._stats[pool._order[0]].status = "dead"
    s = pool.snapshot()
    assert s.total == 3 and s.dead == 1 and s.healthy == 2
