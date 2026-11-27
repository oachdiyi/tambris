"""Streaming input reader with byte-offset checkpoints and incremental dedup.

Reads the input file line-by-line from a persisted byte offset. URLs are
deduplicated against the session database (SQLite) with an in-memory
write-behind cache, so tens of millions of lines never need to fit in RAM.
"""

from __future__ import annotations

import os
from typing import Iterator, List, Optional, Tuple

from .urlproc import normalize_url, UrlError
from .session import SessionDB


class StreamingUrlReader:
    """Yields (byte_offset_after_line, normalized_url) tuples.

    `byte_offset_after_line` is the file position immediately after the
    returned line, usable as a resume checkpoint. Only one line is held in
    memory at a time; dedup is delegated to the caller-provided predicate.
    """

    def __init__(self, path: str, start_offset: int = 0, chunk: int = 1 << 20):
        self.path = path
        self.offset = start_offset
        self.chunk = chunk
        self.size = os.path.getsize(path)
        self._fh = open(path, "rb")

    def __iter__(self) -> Iterator[Tuple[int, Optional[str]]]:
        fh = self._fh
        fh.seek(self.offset)
        if self.offset > 0:
            # If the checkpoint sits mid-line (interrupted write), realign to
            # the next line start; a clean line boundary is kept as-is.
            fh.seek(self.offset - 1)
            prev = fh.read(1)
            if prev != b"\n":
                fh.readline()
            self.offset = fh.tell()
        pending = b""
        while True:
            chunk = fh.read(self.chunk)
            if not chunk:
                break
            pos = fh.tell() - len(chunk)
            pending += chunk
            lines = pending.split(b"\n")
            pending = lines.pop()
            for line in lines:
                pos += len(line) + 1
                self.offset = pos
                yield self.offset, line
        if pending.strip():
            self.offset = fh.tell()
            yield self.offset, pending
        yield self.size, None

    def lines(self) -> Iterator[Tuple[int, Optional[str]]]:
        yield from self

    def close(self) -> None:
        self._fh.close()


class Deduplicator:
    """Incremental dedup: bounded in-memory cache + SQLite persistence."""

    def __init__(self, db: SessionDB, enabled: bool = True, cache_size: int = 100_000):
        self.db = db
        self.enabled = enabled
        self.cache_size = cache_size
        self._recent: dict[str, None] = {}
        self._batch: List[str] = []

    def is_duplicate(self, url: str) -> bool:
        if not self.enabled:
            return False
        if url in self._recent:
            return True
        if self.db.seen_contains(url):
            return True
        self._recent[url] = None
        if len(self._recent) > self.cache_size:
            for k in list(self._recent.keys())[: self.cache_size // 2]:
                del self._recent[k]
        self._batch.append(url)
        if len(self._batch) >= 2000:
            self.flush()
        return False

    def flush(self) -> None:
        if self._batch:
            self.db.mark_seen(self._batch)
            self._batch.clear()

    def mark_processed(self, url: str) -> None:
        if self.enabled and url not in self._recent:
            self.db.mark_seen([url])


def stream_normalized_urls(
    reader: StreamingUrlReader,
    dedup: Deduplicator,
    prefer_https: bool = True,
) -> Iterator[Tuple[int, Optional[str]]]:
    """Wrap a reader: yields (checkpoint_offset, url) for valid unique URLs.

    Skips blank lines, comments and invalid entries. The yielded offset is the
    byte position after the source line that produced the URL.
    """
    for offset, raw_line in reader:
        if raw_line is None:
            yield offset, None
            return
        line = raw_line.decode("utf-8", "replace").strip()
        if not line or line.startswith("#") or line.startswith("//"):
            continue
        try:
            url = normalize_url(line, prefer_https=prefer_https)
        except UrlError:
            continue
        if dedup.is_duplicate(url):
            continue
        yield offset, url
