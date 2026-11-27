"""Session runner: streaming loader, worker pool, checkpoints, deferral."""

from __future__ import annotations

import asyncio
import os
import time
from typing import Callable, Dict, List, Optional

from . import __version__
from .checker import Checker, CheckResult
from .config import Settings
from .input_stream import Deduplicator, StreamingUrlReader, stream_normalized_urls
from .proxies import ProxyPool
from .results import OutputWriter
from .session import SessionDB
from .stats import Stats

MAX_DEFER_ROUNDS = 3
PENDING_BATCH = 2000


class Runner:
    def __init__(
        self,
        settings: Settings,
        db: SessionDB,
        pool: Optional[ProxyPool],
        writer: OutputWriter,
        input_path: str,
    ):
        self.s = settings
        self.db = db
        self.pool = pool
        self.writer = writer
        self.input_path = input_path
        self.checker = Checker(settings, pool)
        self.stats = Stats(total_urls=0)
        if pool is not None:
            pool.set_per_proxy_limit(settings.per_proxy_concurrency)
        self.discovered = 0
        self.paused = False
        self.stopped = False
        self.error: Optional[str] = None
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=max(settings.concurrency * 2, 100))
        self._deferred: List[str] = []
        self._defer_rounds: Dict[str, int] = {}
        self._done_batch: List[str] = []
        self._pending_batch: List[str] = []
        self._last_checkpoint = time.monotonic()
        self._file_size = os.path.getsize(input_path)
        self._read_offset = 0
        self._loader_done = False
        self._loader_task: Optional[asyncio.Task] = None
        self._tasks: List[asyncio.Task] = []

    # ------------------------------------------------------------------ #

    async def run(self, resume: bool = False,
                  progress_cb: Optional[Callable[[], None]] = None) -> None:
        if self.pool is not None and not self.pool._detected:
            await self.pool.resolve_schemes()
        offset = 0
        if resume:
            offset = self.db.get_int("byte_offset", 0)
            self._read_offset = offset
            await self._requeue_pending()

        dedup = Deduplicator(self.db, enabled=self.s.dedup)
        reader = StreamingUrlReader(self.input_path, start_offset=offset)
        loader = asyncio.create_task(self._load(reader, dedup))
        self._loader_task = loader

        workers = [
            asyncio.create_task(self._worker(i))
            for i in range(max(self.s.concurrency, 1))
        ]
        maintainer = asyncio.create_task(self._maintain())
        self.stats.total_urls = self.discovered + self.db.pending_count()
        try:
            await loader
            self._loader_done = True
            # wait until the queue drains
            await self._queue.join()
            # drain deferred URLs: retry while any proxy is healthy; pause
            # (never misclassify) while none are
            while self._deferred and not self.stopped:
                if self.pool is None or self.pool.healthy_count() > 0:
                    self.paused = False
                    requeue, self._deferred = self._deferred, []
                    for url in requeue:
                        await self._queue.put(url)
                    await self._queue.join()
                elif self.pool.recoverable_count() == 0:
                    # every proxy is permanently dead: defer the remaining
                    # URLs to proxy_failures.txt; they stay in the pending
                    # table and are retried on the next resume
                    self.paused = False
                    remaining, self._deferred = self._deferred, []
                    for url in remaining:
                        self.writer.write_proxy_failure(
                            url, None, "no working proxies remain"
                        )
                else:
                    self.paused = True
                    await asyncio.sleep(0.5)
            self.paused = False
            await self._final_checkpoint()
        except asyncio.CancelledError:
            self.stopped = True
        finally:
            for t in workers + [maintainer, loader]:
                t.cancel()
            await asyncio.gather(*workers, maintainer, return_exceptions=True)
            try:
                reader.close()
            except Exception:
                pass
            await self.checker.close()
            self._maybe_checkpoint(force=True)
            self.writer.flush()
            self.db.commit()

    async def _load(self, reader: StreamingUrlReader, dedup: Deduplicator) -> None:
        loop = asyncio.get_running_loop()
        gen = stream_normalized_urls(
            reader, dedup, prefer_https=self.s.prefer_https
        )
        while True:
            offset, url = await loop.run_in_executor(None, next, gen, (None, None))
            if url is None:
                self._read_offset = offset if offset else self._file_size
                break
            self.discovered += 1
            self.stats.total_urls = self.discovered + self.db.pending_count()
            self._pending_batch.append(url)
            await self._queue.put(url)
            if len(self._pending_batch) >= PENDING_BATCH:
                self.db.add_pending(self._pending_batch)
                self._pending_batch.clear()
                self._read_offset = offset
                self._maybe_checkpoint(force=True)
            else:
                self._read_offset = offset

    async def _requeue_pending(self) -> None:
        """Push URLs left in the pending table (crash/deferral leftovers)
        back into the work queue without re-reading the input file."""
        while True:
            urls = self.db.pop_pending(10_000)
            if not urls:
                break
            for url in urls:
                self.discovered += 1
                await self._queue.put(url)

    async def _worker(self, idx: int) -> None:
        while True:
            url = await self._queue.get()
            try:
                self.stats.active_workers += 1
                result = await self.checker.check(url)
                self._handle_result(url, result)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # defensive: worker must survive
                self.error = f"worker error: {exc}"
            finally:
                self.stats.active_workers -= 1
                self._queue.task_done()

    def _handle_result(self, url: str, result: CheckResult) -> None:
        if result.deferred:
            rounds = self._defer_rounds.get(url, 0) + 1
            self._defer_rounds[url] = rounds
            if rounds < MAX_DEFER_ROUNDS:
                self._deferred.append(url)
                return
            # give up for this run: stays in pending table for resume
            self.writer.write_result(result)
            return
        self._defer_rounds.pop(url, None)
        self.db.save_result(
            url=url,
            final_url=result.final_url,
            status=result.category,
            category=result.category,
            status_code=result.status_code,
            reason=result.reason,
            proxy=result.proxy,
        )
        self.db.conn.execute("DELETE FROM pending_urls WHERE url=?", (url,))
        self._done_batch.append(url)
        self.writer.write_result(result)
        self.stats.record(result.category)

    async def _maintain(self) -> None:
        """Re-queue deferred URLs when proxies recover; checkpoint on schedule."""
        while True:
            await asyncio.sleep(1.0)
            if self._deferred and self.pool is not None:
                if self.pool.healthy_count() > 0:
                    self.paused = False
                    requeue, self._deferred = self._deferred[:], []
                    for url in requeue:
                        await self._queue.put(url)
                else:
                    self.paused = True
            elif not self._deferred:
                self.paused = False
            self._maybe_checkpoint()

    def _maybe_checkpoint(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_checkpoint < self.s.checkpoint_interval:
            return
        self._last_checkpoint = now
        if self._pending_batch:
            self.db.add_pending(self._pending_batch)
            self._pending_batch.clear()
        if self._done_batch:
            self.db.conn.executemany(
                "DELETE FROM pending_urls WHERE url=?",
                ((u,) for u in self._done_batch),
            )
            self._done_batch.clear()
        self.db.checkpoint(self._read_offset, self.s.to_json())
        self.writer.flush()

    async def _final_checkpoint(self) -> None:
        if self._pending_batch:
            self.db.add_pending(self._pending_batch)
            self._pending_batch.clear()
        self.db.checkpoint(self._file_size, self.s.to_json())
        self.writer.flush()
        self.db.commit()

    # ------------------------------------------------------------------ #

    def snapshot(self) -> Dict:
        remaining = 0
        try:
            remaining = self._queue.qsize() + len(self._deferred)
        except Exception:
            pass
        if self._loader_done:
            remaining = max(self.stats.total_urls - self.stats.processed, 0)
        snap = {
            "processed": self.stats.processed,
            "total": self.stats.total_urls,
            "discovered": self.discovered,
            "remaining": remaining,
            "by_category": dict(self.stats.by_category),
            "elapsed": self.stats.elapsed(),
            "eta": self.stats.eta_seconds(),
            "cpm": self.stats.sample(),
            "avg_cpm": self.stats.average_cpm(),
            "peak_cpm": self.stats.peak_cpm,
            "active_workers": self.stats.active_workers,
            "paused": self.paused,
            "read_offset": self._read_offset,
            "file_size": self._file_size,
            "proxy_failures": getattr(self.pool, "total_proxy_failures", 0)
            if self.pool
            else 0,
            "pool": self.pool.snapshot() if self.pool else None,
            "error": self.error,
            "version": __version__,
            "mode": self.s.mode,
            "preset": self.s.preset,
        }
        return snap

    def stop(self) -> None:
        self.stopped = True
        self._maybe_checkpoint(force=True)
