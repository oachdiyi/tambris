"""Proxy integration tests through live local proxy fixtures."""

from __future__ import annotations

import asyncio
import socket

import pytest

from sitecheck.checker import Checker, DEFERRED
from sitecheck.classify import S_VALID, S_DEAD, S_ERROR
from sitecheck.proxies import ProxyPool, ProxySpec, parse_proxy_line


def make_checker(tmp_path, pool=None, **overrides):
    from tests.conftest import make_settings

    settings = make_settings(tmp_path, **overrides)
    return Checker(settings, pool=pool), settings


def pool_of(specs, **kw) -> ProxyPool:
    parsed = [parse_proxy_line(s) for s in specs]
    return ProxyPool([p for p in parsed if p], **kw)


async def _dead_tcp_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# ------------------------------------------------------ protocol support

async def test_http_proxy_plain_request(tmp_path, http_proxy, target_server):
    pool = pool_of([f"http://127.0.0.1:{http_proxy.port}"])
    checker, _ = make_checker(tmp_path, pool=pool)
    result = await checker.check(f"{target_server}/ok")
    assert result.category == S_VALID
    assert result.proxy is not None
    assert "http" in result.proxy
    assert http_proxy.request_count >= 1


async def test_https_via_http_proxy_connect_tunnel(tmp_path, http_proxy, https_server):
    https_url, ctx = https_server
    pool = pool_of([f"http://127.0.0.1:{http_proxy.port}"])
    checker, s = make_checker(tmp_path, pool=pool)
    s.verify_tls = False  # test server uses a self-signed certificate
    result = await checker.check(f"{https_url}/ok")
    assert result.category == S_VALID
    assert http_proxy.tunnel_count >= 1


async def test_socks5_proxy(tmp_path, socks5_proxy, target_server):
    pool = pool_of([socks5_proxy.url()])
    checker, _ = make_checker(tmp_path, pool=pool)
    result = await checker.check(f"{target_server}/ok")
    assert result.category == S_VALID
    assert socks5_proxy.connect_count >= 1


async def test_socks4_proxy(tmp_path, socks4_proxy, target_server):
    pool = pool_of([socks4_proxy.url()])
    checker, _ = make_checker(tmp_path, pool=pool)
    result = await checker.check(f"{target_server}/ok")
    assert result.category == S_VALID
    assert socks4_proxy.connect_count >= 1


async def test_socks5_auth_ok(tmp_path, socks5_proxy_auth, target_server):
    pool = pool_of([socks5_proxy_auth.url()])
    checker, _ = make_checker(tmp_path, pool=pool)
    result = await checker.check(f"{target_server}/ok")
    assert result.category == S_VALID


async def test_socks5_auth_rejected_never_fails_the_site(
    tmp_path, socks5_proxy_auth, target_server
):
    bad = socks5_proxy_auth.url().replace("user:secret@", "user:wrong@")
    pool = pool_of([bad])
    checker, _ = make_checker(tmp_path, pool=pool, retries=1)
    result = await checker.check(f"{target_server}/ok")
    assert result.category == DEFERRED
    assert result.deferred
    assert result.category != S_DEAD


async def test_http_proxy_auth(tmp_path, http_proxy_auth, target_server):
    pool = pool_of([f"http://user:pass@127.0.0.1:{http_proxy_auth.port}"])
    checker, _ = make_checker(tmp_path, pool=pool)
    result = await checker.check(f"{target_server}/ok")
    assert result.category == S_VALID


async def test_http_proxy_auth_rejected(tmp_path, http_proxy_auth, target_server):
    pool = pool_of([f"http://user:wrong@127.0.0.1:{http_proxy_auth.port}"])
    checker, _ = make_checker(tmp_path, pool=pool, retries=1)
    result = await checker.check(f"{target_server}/ok")
    assert result.category == DEFERRED


async def test_auto_detection_via_pool(http_proxy, socks5_proxy, target_server, tmp_path):
    pool = ProxyPool(
        [
            ProxySpec(None, "127.0.0.1", http_proxy.port),
            ProxySpec(None, "127.0.0.1", socks5_proxy.port),
        ],
        detect_timeout=5,
    )
    await pool.resolve_schemes()
    checker, _ = make_checker(tmp_path, pool=pool)
    for _ in range(4):
        result = await checker.check(f"{target_server}/ok")
        assert result.category == S_VALID


# ------------------------------------------------- failure vs destination

async def test_dead_proxy_then_working_proxy(tmp_path, http_proxy, target_server):
    dead = await _dead_tcp_port()
    pool = pool_of([f"http://127.0.0.1:{dead}", f"http://127.0.0.1:{http_proxy.port}"])
    checker, _ = make_checker(tmp_path, pool=pool, retries=3)
    result = await checker.check(f"{target_server}/ok")
    assert result.category == S_VALID, "URL must succeed via the working proxy"
    assert result.proxy_failures >= 1
    assert result.category != S_DEAD


async def test_dead_proxy_only_is_never_a_site_failure(tmp_path, target_server):
    dead1 = await _dead_tcp_port()
    dead2 = await _dead_tcp_port()
    pool = pool_of([f"http://127.0.0.1:{dead1}", f"http://127.0.0.1:{dead2}"])
    checker, _ = make_checker(tmp_path, pool=pool, retries=2)
    result = await checker.check(f"{target_server}/ok")
    assert result.deferred
    assert result.category == DEFERRED
    assert result.category not in (S_VALID, S_DEAD, S_ERROR)


async def test_destination_404_through_working_proxy(tmp_path, http_proxy, target_server):
    pool = pool_of([f"http://127.0.0.1:{http_proxy.port}"])
    checker, _ = make_checker(tmp_path, pool=pool)
    result = await checker.check(f"{target_server}/status/404")
    assert result.category == S_DEAD
    assert result.proxy_failures == 0


async def test_proxy_generated_error_page_not_trusted(
    tmp_path, http_proxy_error_page, target_server
):
    pool = pool_of([f"http://127.0.0.1:{http_proxy_error_page.port}"])
    checker, _ = make_checker(tmp_path, pool=pool, retries=1)
    result = await checker.check(f"{target_server}/ok")
    assert result.category in (DEFERRED, S_ERROR)
    assert result.category != S_DEAD


async def test_quarantine_rotates_to_healthy_proxy(tmp_path, http_proxy, target_server):
    dead = await _dead_tcp_port()
    pool = pool_of(
        [f"http://127.0.0.1:{dead}", f"http://127.0.0.1:{http_proxy.port}"],
        health_threshold=1,
        quarantine_seconds=60,
    )
    checker, _ = make_checker(tmp_path, pool=pool, retries=5)
    for _ in range(3):
        result = await checker.check(f"{target_server}/ok")
        assert result.category == S_VALID
    snap = pool.snapshot()
    assert snap.dead + snap.quarantined >= 1


async def test_no_proxies_configured_checks_directly(tmp_path, target_server):
    checker, _ = make_checker(tmp_path, pool=None)
    result = await checker.check(f"{target_server}/ok")
    assert result.category == S_VALID


async def test_unreachable_proxy_defers(tmp_path, target_server):
    dead = await _dead_tcp_port()
    pool = pool_of([f"http://127.0.0.1:{dead}"])
    checker, _ = make_checker(tmp_path, pool=pool, retries=0)
    result = await checker.check(f"{target_server}/ok")
    assert result.deferred
    assert result.category == DEFERRED
