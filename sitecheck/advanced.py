"""Advanced Mode classification: accurate website validation with confidence.

HTTP 200 alone never guarantees SUCCESS. Evidence from status, title, visible
text, headers, final URL and fingerprints is combined; when evidence is
insufficient the result is UNKNOWN rather than a guess.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List

from .http_client import Outcome
from . import fingerprints as fp

A_SUCCESS = "SUCCESS"
A_DEAD = "DEAD"
A_RESTRICTED = "RESTRICTED"
A_PARKED = "PARKED"
A_FAILED = "FAILED"
A_UNKNOWN = "UNKNOWN"

KIND_TO_CATEGORY = {
    fp.K_PARKED: A_PARKED,
    fp.K_SOFT404: A_DEAD,
    fp.K_SUSPENDED: A_DEAD,
    fp.K_HOSTING_DEFAULT: A_FAILED,
    fp.K_CDN_ERROR: A_FAILED,
    fp.K_BROWSER_ERROR: A_FAILED,
}

# confidence thresholds
PARKED_THRESHOLD = 50
NEGATIVE_THRESHOLD = 50


@dataclass
class AdvancedVerdict:
    category: str
    reason: str
    confidence: int = 0  # 0-100


def _header_evidence(outcome: Outcome) -> List[str]:
    notes: List[str] = []
    server = outcome.headers.get("server", "").lower()
    if "cloudflare" in server and outcome.headers.get("cf-mitigated"):
        notes.append("cdn challenge")
    return notes


def classify_advanced(outcome: Outcome) -> AdvancedVerdict:
    code = outcome.status_code

    # ---- connection-level destination evidence ----
    if code is None:
        from .http_client import (
            PH_DEST_DNS_NXDOMAIN,
            PH_DEST_DNS_TEMP,
            PH_DEST_TLS,
            PH_DEST_REFUSED,
            PH_DEST_TIMEOUT,
            PH_DEST_RESET,
            PH_AMBIGUOUS,
        )

        phase = outcome.phase
        if phase == PH_DEST_DNS_NXDOMAIN:
            return AdvancedVerdict(A_DEAD, "permanent DNS NXDOMAIN", 95)
        if phase == PH_DEST_DNS_TEMP:
            return AdvancedVerdict(A_UNKNOWN, "temporary DNS failure", 20)
        if phase == PH_DEST_TLS:
            return AdvancedVerdict(A_FAILED, "TLS failure", 70)
        if phase == PH_DEST_REFUSED:
            return AdvancedVerdict(A_FAILED, "connection refused", 80)
        if phase == PH_DEST_TIMEOUT:
            return AdvancedVerdict(A_UNKNOWN, "destination timeout", 25)
        if phase == PH_DEST_RESET:
            return AdvancedVerdict(A_UNKNOWN, "connection reset", 25)
        if phase == PH_AMBIGUOUS:
            return AdvancedVerdict(A_UNKNOWN, "unresolved connection error", 15)
        return AdvancedVerdict(A_UNKNOWN, f"connection error: {outcome.detail}", 10)

    # ---- status-only categories ----
    if 200 <= code < 300:
        return _classify_2xx(outcome)
    if code in (404, 410):
        return AdvancedVerdict(A_DEAD, f"HTTP {code}", 90)
    if code in (401, 403, 405, 429, 451):
        return AdvancedVerdict(A_RESTRICTED, f"HTTP {code}", 85)
    if 400 <= code < 500:
        return AdvancedVerdict(A_FAILED, f"HTTP {code}", 70)
    if 500 <= code < 600:
        return AdvancedVerdict(A_FAILED, f"HTTP {code}", 75)
    if 300 <= code < 400:
        return AdvancedVerdict(A_UNKNOWN, "unresolved redirect", 30)
    return AdvancedVerdict(A_UNKNOWN, f"unexpected status {code}", 20)


def _classify_2xx(outcome: Outcome) -> AdvancedVerdict:
    html = ""
    if outcome.body:
        try:
            html = outcome.body.decode("utf-8", "replace")
        except Exception:
            html = ""
    if not html.strip():
        # 2xx with no body at all: minimal evidence. Empty pages with a real
        # content-length of 0 are usually placeholders; JS-heavy sites come
        # with some HTML shell, so truly empty is suspicious but unproven.
        if outcome.headers.get("content-length", "").strip() in ("0", ""):
            return AdvancedVerdict(A_UNKNOWN, "2xx with empty body", 25)
        return AdvancedVerdict(A_UNKNOWN, "2xx without inspectable body", 25)

    signals = fp.analyze_content(html)
    scores = fp.score_html(signals)

    parked = scores.get(fp.K_PARKED)
    if parked and (parked.title_hit and parked.score >= PARKED_THRESHOLD * 0.8):
        return AdvancedVerdict(
            A_PARKED, f"parked domain (title: {parked.hits[0]})", min(95, 60 + parked.score // 2)
        )
    if parked and parked.score >= PARKED_THRESHOLD + 20:
        return AdvancedVerdict(
            A_PARKED, f"parked domain ({', '.join(parked.hits[:3])})",
            min(95, 55 + parked.score // 2),
        )

    text_rich = fp.looks_text_rich(signals)
    js_heavy = signals.js_markers >= 1

    negative_kinds = [
        fp.K_SOFT404,
        fp.K_HOSTING_DEFAULT,
        fp.K_CDN_ERROR,
        fp.K_BROWSER_ERROR,
        fp.K_SUSPENDED,
    ]
    best_kind = None
    best_score = 0
    for kind in negative_kinds:
        s = scores.get(kind)
        if s and s.score > best_score:
            best_kind, best_score = kind, s.score

    if best_kind is not None:
        entry = scores[best_kind]
        threshold = NEGATIVE_THRESHOLD
        if text_rich:
            threshold += 60  # real content: demand much stronger evidence
        if js_heavy:
            threshold += 25
        # title match is strong corroboration
        strong_title = entry.title_hit and entry.score >= 40
        if strong_title and not text_rich:
            category = KIND_TO_CATEGORY[best_kind]
            return AdvancedVerdict(
                category,
                f"{best_kind} signal: {entry.hits[0]} (title)",
                min(95, 60 + entry.score // 2),
            )
        if entry.score >= threshold:
            category = KIND_TO_CATEGORY[best_kind]
            return AdvancedVerdict(
                category,
                f"{best_kind} signals: {', '.join(entry.hits[:3])}",
                min(90, 50 + entry.score // 2),
            )
        # weak evidence below threshold: not conclusive
        if entry.score >= threshold * 0.5:
            return AdvancedVerdict(
                A_UNKNOWN,
                f"weak {best_kind} signal: {', '.join(entry.hits[:2])}",
                35,
            )

    # no significant negative evidence: judge legitimacy of the content
    if text_rich:
        return AdvancedVerdict(A_SUCCESS, "HTTP 200, substantial real content", 85)
    if js_heavy or signals.link_count >= 3 or signals.text_len >= 200:
        return AdvancedVerdict(
            A_SUCCESS, "HTTP 200, page structure present", 60
        )
    if signals.title:
        return AdvancedVerdict(
            A_UNKNOWN, f"HTTP 200 with minimal content (title: {signals.title[:60]})", 35
        )
    return AdvancedVerdict(A_UNKNOWN, "HTTP 200 with insufficient evidence", 20)
