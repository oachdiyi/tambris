"""Controlled CPM benchmark against a local test server.

This measures raw engine throughput on loopback only. Real-network CPM is
dominated by latency and will be much lower. Run:

    python benchmarks/benchmark.py [n_urls] [concurrency]
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from aiohttp import web
from aiohttp.test_utils import TestServer

from sitecheck.config import Settings
from sitecheck.proxies import ProxyPool
from sitecheck.results import OutputWriter
from sitecheck.runner import Runner
from sitecheck.session import SessionDB
from tests.servers import build_target_app


async def main(n_urls: int = 20000, concurrency: int = 500) -> None:
    server = TestServer(build_target_app())
    await server.start_server()
    base = f"http://127.0.0.1:{server.port}"

    tmp = tempfile.mkdtemp(prefix="sitecheck-bench-")
    input_path = os.path.join(tmp, "urls.txt")
    with open(input_path, "w", encoding="utf-8") as fh:
        for i in range(n_urls):
            fh.write(f"{base}/status/{200 + (i % 9)}?i={i}\n")

    settings = Settings()
    settings.apply_preset("extreme")
    settings.concurrency = concurrency
    settings.head_first = True
    settings.request_timeout = 10.0

    db = SessionDB(os.path.join(tmp, "session.db"))
    writer = OutputWriter(os.path.join(tmp, "output"))
    runner = Runner(settings, db, None, writer, input_path)

    start = time.monotonic()
    await runner.run()
    elapsed = time.monotonic() - start

    cpm = runner.stats.processed * 60.0 / elapsed
    print(f"benchmark (local loopback, not real-network):")
    print(f"  urls        : {runner.stats.processed}")
    print(f"  concurrency : {concurrency}")
    print(f"  elapsed     : {elapsed:.2f}s")
    print(f"  avg CPM     : {cpm:,.0f}")
    print(f"  peak CPM    : {runner.stats.peak_cpm:,.0f}")
    print(f"  workdir     : {tmp}")

    writer.close()
    db.close()
    await server.close()


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 20000
    c = int(sys.argv[2]) if len(sys.argv) > 2 else 500
    asyncio.run(main(n, c))
