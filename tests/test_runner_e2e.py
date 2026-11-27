"""Runner end-to-end tests: full session, crash resume, outputs, stats."""

from __future__ import annotations

import asyncio
import os
import socket
import time

import pytest

from sitecheck.config import Settings
from sitecheck.runner import Runner
from sitecheck.session import SessionDB
from sitecheck.proxies import ProxyPool
from sitecheck.results import OutputWriter


def build_settings(tmp_path, **overrides) -> Settings:
    s = Settings()
    s.output_dir = str(tmp_path / "output")
    s.head_first = False
    s.checkpoint_interval = 0.3
    s.dashboard_fps = 30
    s.concurrency = 8
    for k, v in overrides.items():
        setattr(s, k, v)
    return s


def make_input(tmp_path, urls, name="urls.txt") -> str:
    path = str(tmp_path / name)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(urls) + "\n")
    return path


def make_runner(tmp_path, input_path, settings=None, db_path=None, pool=None):
    settings = settings or build_settings(tmp_path)
    db_path = db_path or str(tmp_path / "session.db")
    db = SessionDB(db_path)
    writer = OutputWriter(settings.output_dir)
    runner = Runner(settings, db, pool, writer, input_path)
    return runner, db_path


def read_output(tmp_path, name):
    path = os.path.join(str(tmp_path / "output"), name)
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as fh:
        return [
            line.rstrip("\n")
            for line in fh
            if line.strip() and not line.startswith("#")
        ]


def dead_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


# ------------------------------------------------------------------ full run

async def test_full_run_categories_and_outputs(tmp_path, target_server):
    urls = [
        f"{target_server}/ok",
        f"{target_server}/status/404",
        f"{target_server}/status/403",
        f"{target_server}/status/500",
        f"{target_server}/redirect/3",
        "garbage !!!",
        f"{target_server}/ok",  # duplicate
    ]
    input_path = make_input(tmp_path, urls)
    runner, db_path = make_runner(tmp_path, input_path)
    await runner.run()

    assert runner.stats.processed == 5  # dupes and invalid skipped
    counts = runner.writer.counts()
    assert counts.get("VALID", 0) + counts.get("SUCCESS", 0) == 2
    assert counts.get("DEAD", 0) == 1
    assert counts.get("RESTRICTED", 0) == 1
    assert counts.get("ERROR", 0) == 1

    success = read_output(tmp_path, "success.txt")
    assert len(success) == 2
    fields = success[0].split("\t")
    assert len(fields) == 7  # url | final | code | cat | reason | proxy | ts
    assert fields[0].startswith(target_server)
    dead = read_output(tmp_path, "dead.txt")
    assert any("\t404\t" in l for l in dead)

    assert any(l.split("\t")[1].endswith("/ok") for l in success)

    db = SessionDB(db_path)
    assert db.get_int("byte_offset") == os.path.getsize(input_path)
    assert db.pending_count() == 0
    db.close()


async def test_no_redirect_saves_original_url(tmp_path, target_server):
    input_path = make_input(tmp_path, [f"{target_server}/ok"])
    runner, _ = make_runner(tmp_path, input_path)
    await runner.run()
    lines = read_output(tmp_path, "success.txt")
    assert lines[0].split("\t")[0] == f"{target_server}/ok"
    assert lines[0].split("\t")[1] == f"{target_server}/ok"


# ------------------------------------------------------------------ resume

async def test_interrupt_and_resume_no_duplicates_no_recount(tmp_path, target_server):
    n = 120
    urls = [f"{target_server}/status/{200 + (i % 5)}?i={i}" for i in range(n)]
    input_path = make_input(tmp_path, urls)
    runner, db_path = make_runner(tmp_path, input_path)

    task = asyncio.create_task(runner.run())
    while runner.stats.processed < 20:
        await asyncio.sleep(0.01)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    partial = runner.stats.processed
    assert 0 < partial < n

    db = SessionDB(db_path)
    offset = db.get_int("byte_offset", 0)
    assert 0 < offset <= os.path.getsize(input_path)
    db.close()

    settings = build_settings(tmp_path)
    runner2, _ = make_runner(tmp_path, input_path, settings=settings, db_path=db_path)
    await runner2.run(resume=True)

    assert runner2.stats.processed == n - partial

    names = ("success.txt", "dead.txt", "restricted.txt", "parked.txt",
             "failed.txt", "errors.txt", "unknown.txt")
    seen = []
    for name in names:
        seen += [l.split("\t")[0] for l in read_output(tmp_path, name)]
    assert len(seen) == n
    assert len(seen) == len(set(seen))


async def test_resume_after_completion_no_reread_no_dupes(tmp_path, target_server):
    urls = [f"{target_server}/ok", f"{target_server}/status/404"]
    input_path = make_input(tmp_path, urls)
    runner, db_path = make_runner(tmp_path, input_path)
    await runner.run()

    runner2, _ = make_runner(tmp_path, input_path, db_path=db_path)
    await runner2.run(resume=True)
    assert runner2.stats.processed == 0
    assert len(read_output(tmp_path, "success.txt")) == 1


async def test_resume_with_changed_preset(tmp_path, target_server):
    urls = [f"{target_server}/ok", f"{target_server}/status/404"]
    input_path = make_input(tmp_path, urls)
    runner, db_path = make_runner(tmp_path, input_path)
    await runner.run()

    settings = build_settings(tmp_path)
    settings.apply_preset("extreme")
    runner2, _ = make_runner(tmp_path, input_path, settings=settings, db_path=db_path)
    await runner2.run(resume=True)
    assert runner2.stats.processed == 0


# ------------------------------------------------------- proxy integration

async def test_runner_with_dead_proxies_defers_not_fails(tmp_path, target_server):
    proxy_file = str(tmp_path / "proxies.txt")
    with open(proxy_file, "w") as fh:
        fh.write(f"127.0.0.1:{dead_port()}\n127.0.0.1:{dead_port()}\n")
    pool = ProxyPool.from_file(proxy_file, health_threshold=1, quarantine_seconds=2)

    input_path = make_input(
        tmp_path, [f"{target_server}/ok", f"{target_server}/status/404"]
    )
    runner, _ = make_runner(tmp_path, input_path, pool=pool)
    runner.s.retries = 1
    await runner.run()

    assert read_output(tmp_path, "dead.txt") == []
    assert read_output(tmp_path, "success.txt") == []
    proxy_lines = read_output(tmp_path, "proxy_failures.txt")
    assert len(proxy_lines) == 2
    db = SessionDB(runner.db.path)
    assert db.pending_count() == 2  # retryable on resume
    db.close()


async def test_runner_with_working_proxy_succeeds(tmp_path, target_server, http_proxy):
    proxy_file = str(tmp_path / "proxies.txt")
    with open(proxy_file, "w") as fh:
        fh.write(f"http://127.0.0.1:{http_proxy.port}\n")
    pool = ProxyPool.from_file(proxy_file)
    input_path = make_input(tmp_path, [f"{target_server}/ok"])
    runner, _ = make_runner(tmp_path, input_path, pool=pool)
    await runner.run()
    success = read_output(tmp_path, "success.txt")
    assert len(success) == 1
    fields = success[0].split("\t")
    assert fields[5].startswith("http://")
    assert read_output(tmp_path, "proxy_failures.txt") == []


# ----------------------------------------------------------------- stats

def test_stats_cpm_accuracy():
    from sitecheck.stats import Stats

    stats = Stats(total_urls=300)
    base = time.monotonic() - 10
    for i in range(100):
        stats.window.append(base + i * 0.1)
    stats.processed = 100
    stats.start_time = base
    avg = stats.average_cpm()
    assert 590 < avg < 610  # 100 items / 10 s = 600 CPM
    cpm = stats.current_cpm()
    assert 590 < cpm < 610
    stats.record("VALID")
    assert stats.processed == 101


def test_stats_eta():
    from sitecheck.stats import Stats

    stats = Stats(total_urls=600)
    stats.start_time = time.monotonic() - 10
    stats.processed = 100  # => 600 CPM
    eta = stats.eta_seconds()
    assert 49 < eta < 51  # 500 remaining / 600 cpm = 50 s
