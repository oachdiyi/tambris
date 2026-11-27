"""Runtime statistics: CPM, peak/average CPM, ETA, worker accounting."""

from __future__ import annotations

import time
from collections import deque
from typing import Dict


class Stats:
    def __init__(self, total_urls: int, window_seconds: float = 30.0):
        self.total_urls = total_urls
        self.processed = 0
        self.by_category: Dict[str, int] = {}
        self.start_time = time.monotonic()
        self.window: deque = deque()  # (timestamp) per processed url
        self.window_seconds = window_seconds
        self.peak_cpm = 0.0
        self._sum_instant_cpm = 0.0
        self._instant_samples = 0
        self.active_workers = 0

    def record(self, category: str) -> None:
        now = time.monotonic()
        self.processed += 1
        self.by_category[category] = self.by_category.get(category, 0) + 1
        self.window.append(now)
        self._trim(now)
        if self.processed % 100 == 0:
            cpm = self.current_cpm()
            if cpm > self.peak_cpm:
                self.peak_cpm = cpm

    def _trim(self, now: float) -> None:
        cutoff = now - self.window_seconds
        while self.window and self.window[0] < cutoff:
            self.window.popleft()

    def current_cpm(self) -> float:
        now = time.monotonic()
        self._trim(now)
        elapsed = max(now - self.window[0], 1e-6) if self.window else 1.0
        count = len(self.window)
        if count < 2:
            return count * 60.0
        return count * 60.0 / elapsed

    def sample(self) -> float:
        cpm = self.current_cpm()
        if cpm > self.peak_cpm:
            self.peak_cpm = cpm
        self._sum_instant_cpm += cpm
        self._instant_samples += 1
        return cpm

    def average_cpm(self) -> float:
        elapsed = max(self.elapsed(), 1e-9)
        return self.processed * 60.0 / elapsed

    def elapsed(self) -> float:
        return time.monotonic() - self.start_time

    def eta_seconds(self) -> float:
        remaining = max(self.total_urls - self.processed, 0)
        avg = self.average_cpm()
        if avg <= 0:
            return float("inf")
        return remaining * 60.0 / avg
