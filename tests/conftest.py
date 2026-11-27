"""Shared fixtures: target servers, proxies, HTTPS, settings."""

from __future__ import annotations

import asyncio
import os
import socket
import ssl
import sys
import tempfile

import pytest
import pytest_asyncio
from aiohttp import web

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from aiohttp.test_utils import TestServer

from tests.servers import (
    HttpProxyServer,
    SocksServer,
    build_target_app,
    make_self_signed_cert,
    ssl_server_context,
)

pytest_plugins = ("pytest_asyncio",)


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest_asyncio.fixture
async def target_server():
    server = TestServer(build_target_app())
    await server.start_server()
    yield f"http://127.0.0.1:{server.port}"
    await server.close()


@pytest_asyncio.fixture
async def https_server():
    with tempfile.TemporaryDirectory() as tmp:
        cert = os.path.join(tmp, "cert.pem")
        key = os.path.join(tmp, "key.pem")
        make_self_signed_cert(cert, key)
        ctx = ssl_server_context(cert, key)
        server = TestServer(build_target_app())
        await server.start_server(ssl=ctx)
        yield f"https://localhost:{server.port}", ctx
        await server.close()


@pytest_asyncio.fixture
async def http_proxy():
    proxy = await HttpProxyServer().start()
    yield proxy
    await proxy.stop()


@pytest_asyncio.fixture
async def http_proxy_auth():
    proxy = await HttpProxyServer(require_auth=True).start()
    yield proxy
    await proxy.stop()


@pytest_asyncio.fixture
async def http_proxy_connect_fail():
    proxy = await HttpProxyServer(refuse_connect=True).start()
    yield proxy
    await proxy.stop()


@pytest_asyncio.fixture
async def http_proxy_error_page():
    proxy = await HttpProxyServer(inject_error_page=True).start()
    yield proxy
    await proxy.stop()


@pytest_asyncio.fixture
async def socks5_proxy():
    proxy = await SocksServer(version=5).start()
    yield proxy
    await proxy.stop()


@pytest_asyncio.fixture
async def socks5_proxy_auth():
    proxy = await SocksServer(version=5, require_auth=True).start()
    yield proxy
    await proxy.stop()


@pytest_asyncio.fixture
async def socks5_proxy_reject():
    proxy = await SocksServer(version=5, reject_all=True).start()
    yield proxy
    await proxy.stop()


@pytest_asyncio.fixture
async def socks4_proxy():
    proxy = await SocksServer(version=4).start()
    yield proxy
    await proxy.stop()


def make_settings(tmp_path, **overrides):
    from sitecheck.config import Settings

    s = Settings()
    s.output_dir = str(tmp_path / "output")
    s.head_first = False  # deterministic for tests unless overridden
    s.dashboard_fps = 30
    for k, v in overrides.items():
        setattr(s, k, v)
    return s
