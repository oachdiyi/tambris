"""Simple Mode classification: HTTP status codes only. Maximum speed."""

from __future__ import annotations

from typing import Optional

from .http_client import (
    Outcome,
    PH_DEST_DNS_NXDOMAIN,
    PH_DEST_DNS_TEMP,
    PH_DEST_TLS,
    PH_DEST_REFUSED,
    PH_DEST_TIMEOUT,
    PH_DEST_RESET,
    PH_AMBIGUOUS,
)

# Simple-mode categories
S_VALID = "VALID"
S_DEAD = "DEAD"
S_RESTRICTED = "RESTRICTED"
S_ERROR = "ERROR"

_VALID = set(range(200, 300))
_DEAD = {404, 410}
_RESTRICTED = {401, 403, 405, 429, 451}
_ERROR_4XX = {400}


def classify_simple(outcome: Outcome) -> tuple[str, str]:
    """Return (category, reason) from a destination-evidence Outcome.

    Caller guarantees `outcome.is_destination_evidence` OR a DNS/TLS
    destination-phase failure.
    """
    code = outcome.status_code
    if code is None:
        phase = outcome.phase
        if phase == PH_DEST_DNS_NXDOMAIN:
            return S_DEAD, "permanent DNS NXDOMAIN"
        if phase == PH_DEST_DNS_TEMP:
            return S_ERROR, "temporary DNS failure"
        if phase == PH_DEST_TLS:
            return S_ERROR, "TLS failure"
        if phase == PH_DEST_REFUSED:
            return S_ERROR, "connection refused"
        if phase == PH_DEST_TIMEOUT:
            return S_ERROR, "destination timeout"
        if phase == PH_DEST_RESET:
            return S_ERROR, "connection reset"
        if phase == PH_AMBIGUOUS:
            return S_ERROR, "unresolved connection error"
        return S_ERROR, f"connection error: {outcome.detail}"
    if code in _VALID:
        return S_VALID, f"HTTP {code}"
    if code in _DEAD:
        return S_DEAD, f"HTTP {code}"
    if code in _RESTRICTED:
        return S_RESTRICTED, f"HTTP {code}"
    if code in _ERROR_4XX:
        return S_ERROR, f"HTTP {code}"
    if 400 <= code < 500:
        return S_ERROR, f"HTTP {code}"
    if 500 <= code < 600:
        return S_ERROR, f"HTTP {code}"
    return S_ERROR, f"unexpected status {code}"


def simple_head_verdict(outcome: Outcome) -> Optional[tuple[str, str]]:
    """Classify a HEAD response when it is conclusive on its own."""
    if outcome.status_code is not None:
        return classify_simple(outcome)
    return None
