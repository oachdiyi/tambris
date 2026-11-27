"""Output writers: structured result files, flushed incrementally.

Proxy failures are written to proxy_failures.txt only; they never touch the
website category files.
"""

from __future__ import annotations

import os
import time
from typing import Dict, Optional

from .checker import DEFERRED

# category -> filename
FILES = {
    "VALID": "success.txt",
    "SUCCESS": "success.txt",
    "DEAD": "dead.txt",
    "RESTRICTED": "restricted.txt",
    "PARKED": "parked.txt",
    "FAILED": "failed.txt",
    "ERROR": "errors.txt",
    "UNKNOWN": "unknown.txt",
}

HEADER = "# url | final_url | status_code | category | reason | proxy | ts"


def format_line(
    url: str,
    final_url: str,
    status_code: Optional[int],
    category: str,
    reason: str,
    proxy: Optional[str],
    ts: Optional[float] = None,
) -> str:
    return "\t".join(
        [
            url,
            final_url,
            str(status_code if status_code is not None else "-"),
            category,
            reason.replace("\t", " ").replace("\n", " "),
            proxy or "-",
            time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ts or time.time())),
        ]
    )


class OutputWriter:
    """Append-only writers with small buffers; explicit flush keeps results
    durable across crashes without hammering the disk."""

    def __init__(self, output_dir: str, flush_interval: float = 1.0):
        self.dir = output_dir
        self.flush_interval = flush_interval
        os.makedirs(output_dir, exist_ok=True)
        self._files: Dict[str, object] = {}
        self._counts: Dict[str, int] = {}
        self._last_flush = time.monotonic()

    def _file_for(self, name: str):
        fh = self._files.get(name)
        if fh is None:
            path = os.path.join(self.dir, name)
            new = not os.path.exists(path) or os.path.getsize(path) == 0
            fh = open(path, "a", encoding="utf-8", buffering=8192)
            if new:
                fh.write(HEADER + "\n")
            self._files[name] = fh
        return fh

    def write_result(self, result) -> None:
        """result: checker.CheckResult"""
        category = result.category
        if result.deferred or category == DEFERRED:
            name = "proxy_failures.txt"
        else:
            name = FILES.get(category, "unknown.txt")
        line = format_line(
            result.url,
            result.final_url,
            result.status_code,
            category,
            result.reason,
            result.proxy,
        )
        fh = self._file_for(name)
        fh.write(line + "\n")
        if result.deferred or category == DEFERRED:
            self._counts["proxy_failures"] = self._counts.get("proxy_failures", 0) + 1
        else:
            self._counts[category] = self._counts.get(category, 0) + 1
        self.maybe_flush()

    def write_proxy_failure(self, url: str, proxy: Optional[str], reason: str) -> None:
        fh = self._file_for("proxy_failures.txt")
        fh.write(format_line(url, url, None, "PROXY_FAILURE", reason, proxy) + "\n")
        self._counts["proxy_failures"] = self._counts.get("proxy_failures", 0) + 1

    def maybe_flush(self) -> None:
        now = time.monotonic()
        if now - self._last_flush >= self.flush_interval:
            self.flush()

    def flush(self) -> None:
        for fh in self._files.values():
            fh.flush()
        self._last_flush = time.monotonic()

    def close(self) -> None:
        self.flush()
        for fh in self._files.values():
            try:
                fh.close()
            except Exception:
                pass
        self._files.clear()

    def counts(self) -> Dict[str, int]:
        return dict(self._counts)
