"""Streaming input, dedup, byte-offset resume, and session DB tests."""

from __future__ import annotations

import os
import time

import pytest

from sitecheck.session import SessionDB
from sitecheck.input_stream import (
    Deduplicator,
    StreamingUrlReader,
    stream_normalized_urls,
)
from sitecheck.urlproc import normalize_url


def write_file(path, lines):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def test_reader_offsets_resume_midstream(tmp_path):
    path = str(tmp_path / "urls.txt")
    lines = [f"https://example{i}.com" for i in range(5000)]
    write_file(path, lines)

    reader = StreamingUrlReader(path)
    got = list(reader)
    reader.close()
    offsets = [o for o, _ in got]
    assert offsets == sorted(offsets)
    assert offsets[-1] == os.path.getsize(path)
    assert len([1 for _, l in got if l]) == 5000

    # resume from the offset recorded after line 4321: the next yielded line
    # must be line 4321's successor, never a recount from the start
    target = 4320  # 0-based index of line 4321
    reader = StreamingUrlReader(path, start_offset=offsets[target])
    first = next(iter(reader))
    reader.close()
    assert first[1].decode().strip() == lines[target + 1]


def test_reader_resume_aligns_partial_line(tmp_path):
    path = str(tmp_path / "urls.txt")
    lines = ["https://aaa.com", "https://bbb.com", "https://ccc.com"]
    write_file(path, lines)
    with open(path, "rb") as fh:
        data = fh.read()
    mid = data.index(b"https://bbb.com") + 7  # mid-line
    reader = StreamingUrlReader(path, start_offset=mid)
    items = list(reader)
    reader.close()
    payload = [l.decode().strip() for _, l in items if l]
    assert "https://ccc.com" in payload


def test_dedup_incremental(tmp_path):
    db = SessionDB(str(tmp_path / "s.db"))
    dedup = Deduplicator(db)
    assert not dedup.is_duplicate("https://a.com")
    assert dedup.is_duplicate("https://a.com")
    assert not dedup.is_duplicate("https://b.com")
    dedup.flush()
    db.close()

    db2 = SessionDB(str(tmp_path / "s.db"))
    dedup2 = Deduplicator(db2)
    assert dedup2.is_duplicate("https://a.com")
    assert not dedup2.is_duplicate("https://c.com")
    db2.close()


def test_stream_normalized_skips_invalid_and_dupes(tmp_path):
    path = str(tmp_path / "urls.txt")
    write_file(
        path,
        [
            "# comment",
            "",
            "https://a.com",
            "https://a.com",
            "garbage line !!!",
            "b.com",
            "http://c.com/x",
        ],
    )
    db = SessionDB(str(tmp_path / "s.db"))
    dedup = Deduplicator(db)
    reader = StreamingUrlReader(path)
    items = [
        (o, u) for o, u in stream_normalized_urls(reader, dedup) if u is not None
    ]
    reader.close()
    urls = [u for _, u in items]
    assert urls == ["https://a.com/", "https://b.com/", "http://c.com/x"]
    db.close()


def test_large_file_streaming_and_dedup(tmp_path):
    path = str(tmp_path / "big.txt")
    n = 100_000
    with open(path, "w", encoding="utf-8") as fh:
        chunk = "\n".join(f"https://site{i}.example" for i in range(50_000))
        for _ in range(n // 50_000):
            fh.write(chunk + "\n")
    db = SessionDB(str(tmp_path / "s.db"))
    dedup = Deduplicator(db, cache_size=10_000)
    reader = StreamingUrlReader(path, chunk=64 * 1024)
    count = 0
    last_offset = 0
    for offset, url in stream_normalized_urls(reader, dedup):
        if url is not None:
            count += 1
        last_offset = offset
    reader.close()
    assert count == 50_000  # 200k lines, 50k unique
    assert last_offset == os.path.getsize(path)
    db.close()


def test_session_db_checkpoint_and_results(tmp_path):
    db = SessionDB(str(tmp_path / "s.db"))
    db.checkpoint(1234, "{}")
    assert db.get_int("byte_offset") == 1234
    db.save_result("https://a.com", "https://a.com/", "VALID", "VALID", 200, "HTTP 200", None)
    db.commit()
    assert db.counts() == {"VALID": 1}
    assert db.pending_count() == 0
    db.add_pending(["https://b.com"])
    db.commit()
    assert db.pending_count() == 1
    assert db.pop_pending(10) == ["https://b.com"]
    db.close()


def test_results_persisted_and_never_recounted(tmp_path):
    db = SessionDB(str(tmp_path / "s.db"))
    for i in range(100):
        db.save_result(f"https://{i}.com", None, "VALID", "VALID", 200, "ok", None)
    db.commit()
    db.close()
    db2 = SessionDB(str(tmp_path / "s.db"))
    assert db2.counts()["VALID"] == 100
    db2.close()
