"""URL checking orchestration.

Implements the mandated retry contract: a URL is assigned to a proxy; if the
proxy itself fails, the SAME URL is retried on another working proxy until a
reliable destination response is obtained or the retry limit is hit. Proxy
failures never become website results. When no working proxy remains, the URL
is deferred (not classified).
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import List, Optional

from .http_client import (
    HttpClient,
    Outcome,
    PH_DEST_RESPONSE,
    PH_DEST_DNS_NXDOMAIN,
    PH_DEST_DNS_TEMP,
    PH_DEST_TLS,
    PH_DEST_TIMEOUT,
    PH_DEST_RESET,
    PH_PROXY_TIMEOUT,
    PH_PROXY_UNREACHABLE,
    PH_PROXY_AUTH,
    PH_PROXY_CONNECT,
    PH_AMBIGUOUS,
)
from .proxies import ProxyPool, ProxySpec
from .classify import classify_simple, S_VALID
from .advanced import classify_advanced, AdvancedVerdict

DEFERRED = "DEFERRED"


@dataclass
class CheckResult:
    url: str
    final_url: str
    category: str
    reason: str
    status_code: Optional[int] = None
    proxy: Optional[str] = None
    proxy_failures: int = 0
    deferred: bool = False
    elapsed: float = 0.0


class Checker:
    def __init__(self, settings, pool: Optional[ProxyPool] = None):
        self.s = settings
        self.pool = pool
        self.client = HttpClient(settings)
        self._sem = asyncio.Semaphore(settings.concurrency)

    # ------------------------------------------------------------------ #

    async def close(self) -> None:
        await self.client.close()

    async def check(self, url: str) -> CheckResult:
        async with self._sem:
            return await self._check_inner(url)

    async def _check_inner(self, url: str) -> CheckResult:
        start = time.monotonic()
        excluded: List[str] = []
        proxy_failures = 0
        last_outcome: Optional[Outcome] = None

        attempts = (self.s.retries + 1) if (self.pool and self.pool.has_proxies) else 1
        for attempt in range(attempts):
            spec: Optional[ProxySpec] = None
            if self.pool and self.pool.has_proxies:
                spec = await self.pool.pick(exclude=excluded)
                if spec is None:
                    # No working proxy remains: defer, never misclassify.
                    return CheckResult(
                        url=url,
                        final_url=url,
                        category=DEFERRED,
                        reason="no working proxy available",
                        proxy_failures=proxy_failures,
                        deferred=True,
                        elapsed=time.monotonic() - start,
                    )
                excluded.append(spec.key)
                self.pool.report_request_start(spec)

            outcome = await self._one_attempt(url, spec)
            outcome.proxy_display = spec.display if spec is not None else None
            last_outcome = outcome

            if self.pool and spec is not None:
                self._report(spec, outcome)

            if outcome.is_destination_evidence or outcome.phase in (
                PH_DEST_TLS,
                PH_DEST_DNS_NXDOMAIN,
                PH_DEST_DNS_TEMP,
            ):
                break
            if outcome.is_proxy_failure:
                proxy_failures += 1
                continue
            # ambiguous: retry on another proxy if possible
            if attempt < attempts - 1:
                continue
            break

        elapsed = time.monotonic() - start
        if last_outcome is None:
            return CheckResult(url, url, DEFERRED, "no attempt executed",
                               deferred=True, elapsed=elapsed)

        direct_attempt = self.pool is None or not self.pool.has_proxies
        dest_evidence = (
            last_outcome.is_destination_evidence
            or last_outcome.phase
            in (PH_DEST_TLS, PH_DEST_DNS_NXDOMAIN, PH_DEST_DNS_TEMP)
            or direct_attempt  # without proxies every failure is destination-side
        )
        if not dest_evidence:
            # every attempt ended in a proxy-side or ambiguous failure:
            # the URL stays unresolved and retryable, never a site verdict
            return CheckResult(
                url=url,
                final_url=last_outcome.final_url or url,
                category=DEFERRED,
                reason=f"unresolved after {proxy_failures} proxy failure(s): "
                f"{last_outcome.phase} ({last_outcome.detail})",
                proxy_failures=proxy_failures,
                deferred=True,
                elapsed=elapsed,
            )

        verdict = self._classify(last_outcome)
        if "HTTP fallback used" in (last_outcome.detail or ""):
            verdict.reason += " (TLS failed; HTTP fallback used)"
        if verdict.category == DEFERRED:
            return CheckResult(
                url=url,
                final_url=last_outcome.final_url or url,
                category=DEFERRED,
                reason=verdict.reason,
                proxy_failures=proxy_failures,
                deferred=True,
                elapsed=elapsed,
            )
        return CheckResult(
            url=url,
            final_url=last_outcome.final_url or url,
            category=verdict.category,
            reason=verdict.reason,
            status_code=last_outcome.status_code,
            proxy=self._used_proxy_display(last_outcome),
            proxy_failures=proxy_failures,
            elapsed=elapsed,
        )

    # ------------------------------------------------------------------ #

    def _used_proxy_display(self, outcome: Outcome) -> Optional[str]:
        return getattr(outcome, "proxy_display", None)

    def _report(self, spec: ProxySpec, outcome: Outcome) -> None:
        if outcome.phase == PH_DEST_RESPONSE:
            self.pool.report_destination_response(spec, outcome.elapsed)
        elif outcome.phase in (PH_PROXY_UNREACHABLE, PH_PROXY_AUTH, PH_PROXY_CONNECT):
            self.pool.report_proxy_failure(spec, kind="connect")
        elif outcome.phase == PH_PROXY_TIMEOUT:
            self.pool.report_proxy_failure(spec, kind="timeout")
        elif outcome.phase in (PH_AMBIGUOUS, PH_DEST_RESET):
            self.pool.report_ambiguous(spec)
        else:
            # destination-level transport facts (DNS, refused, dest timeout,
            # TLS) still prove the proxy tunnel works
            self.pool.report_success(spec, outcome.elapsed)

    async def _one_attempt(self, url: str, spec: Optional[ProxySpec]) -> Outcome:
        if self.s.mode == "advanced":
            method = "GET"
        else:
            method = "HEAD" if self.s.head_first else "GET"
        outcome = await self.client.request_chain(url, spec, method)

        if outcome.status_code == 407 and spec is not None:
            # 407 can only be produced by a proxy: authentication rejected
            return Outcome(
                phase=PH_PROXY_AUTH,
                detail="proxy auth rejected (407)",
                elapsed=outcome.elapsed,
            )

        if (
            outcome.is_destination_evidence
            and outcome.body
            and self._looks_like_proxy_error_page(outcome)
        ):
            # likely a proxy- or interceptor-generated error page served as
            # 200: not destination evidence; stay retryable
            return Outcome(
                phase=PH_AMBIGUOUS,
                detail="suspected proxy-generated error page",
                elapsed=outcome.elapsed,
            )

        if method == "HEAD" and self._head_needs_get(outcome):
            get_outcome = await self.client.request_chain(url, spec, "GET")
            if get_outcome.is_destination_evidence and get_outcome.status_code is not None:
                return get_outcome
            if outcome.status_code is None and get_outcome.status_code is not None:
                return get_outcome
            return outcome

        if (
            self.s.https_fallback_http
            and outcome.phase == PH_DEST_TLS
            and url.lower().startswith("https://")
        ):
            fallback = "http://" + url[len("https://"):]
            http_outcome = await self.client.request_chain(fallback, spec, method)
            if http_outcome.is_destination_evidence:
                http_outcome.detail = f"TLS failed; HTTP fallback used ({http_outcome.detail})"
                return http_outcome
        return outcome

    @staticmethod
    def _looks_like_proxy_error_page(outcome: Outcome) -> bool:
        from .http_client import BROWSER_ERROR_MARKERS

        try:
            head = outcome.body[:4096].lower()
        except Exception:
            return False
        return any(marker in head for marker in BROWSER_ERROR_MARKERS)

    @staticmethod
    def _head_needs_get(outcome: Outcome) -> bool:
        if outcome.is_destination_evidence:
            code = outcome.status_code
            if code == 405:
                return True
            if code in (400, 500, 501, 503):
                return True  # some servers mishandle HEAD; verify with GET
            return False
        return outcome.phase in (PH_DEST_TIMEOUT,)

    def _classify(self, outcome: Outcome) -> AdvancedVerdict:
        if self.s.mode == "advanced":
            return classify_advanced(outcome)
        cat, reason = classify_simple(outcome)
        conf = 90 if cat == S_VALID else 80
        return AdvancedVerdict(cat, reason, conf)



