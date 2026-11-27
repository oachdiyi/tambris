"""Asynchronous HTTP checking layer.

Responsibility: obtain a *reliable destination response* through a proxy or
directly, and unambiguously separate proxy-side failures from destination-side
facts.

Failure taxonomy (`Outcome.phase`):
  - PH_PROXY_*   proxy-side; the same URL is retried on another proxy
  - PH_AMBIGUOUS tunnel dropped mid-flight etc.; URL stays unresolved/retryable
  - PH_DEST_*    genuine destination evidence; classify immediately

Sessions and TLS contexts are created once and reused: direct requests and
HTTP-proxy requests share pooled sessions, every SOCKS proxy gets its own
cached session (its connector is proxy-bound), which keeps connection reuse
high and per-request setup cost near zero.
"""

from __future__ import annotations

import asyncio
import ssl
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import aiohttp
from aiohttp import ClientTimeout, TCPConnector

try:
    from aiohttp_socks import ProxyConnector, ProxyType

    _HAVE_SOCKS = True
except Exception:  # pragma: no cover
    _HAVE_SOCKS = False

from .proxies import ProxySpec
from .urlproc import resolve as _resolve

# phases
PH_DEST_RESPONSE = "dest_response"
PH_PROXY_UNREACHABLE = "proxy_unreachable"
PH_PROXY_AUTH = "proxy_auth_rejected"
PH_PROXY_CONNECT = "proxy_connect_failure"
PH_PROXY_TIMEOUT = "proxy_timeout"
PH_DEST_REFUSED = "dest_refused"
PH_DEST_TIMEOUT = "dest_timeout"
PH_DEST_DNS_NXDOMAIN = "dest_dns_nxdomain"
PH_DEST_DNS_TEMP = "dest_dns_temporary"
PH_DEST_TLS = "dest_tls_failure"
PH_DEST_RESET = "dest_reset"
PH_AMBIGUOUS = "ambiguous"

# substrings that mark a 2xx body as a browser/interceptor error page
BROWSER_ERROR_MARKERS = (
    b"this site can't be reached",
    b"this site can\xe2\x80\x99t be reached",
    b"err_name_not_resolved",
    b"err_connection_refused",
    b"err_connection_timed_out",
    b"err_connection_reset",
    b"err_too_many_redirects",
    b"dns_probe_finished_nxdomain",
    b"this page isn't working",
    b"can't reach this page",
)

PROXY_PHASES = {
    PH_PROXY_UNREACHABLE,
    PH_PROXY_AUTH,
    PH_PROXY_CONNECT,
    PH_PROXY_TIMEOUT,
}


@dataclass
class Outcome:
    phase: str
    status_code: Optional[int] = None
    final_url: Optional[str] = None
    history: List[int] = field(default_factory=list)
    headers: Dict[str, str] = field(default_factory=dict)
    body: Optional[bytes] = None
    elapsed: float = 0.0
    detail: str = ""
    redirects: int = 0

    @property
    def is_destination_evidence(self) -> bool:
        return self.phase == PH_DEST_RESPONSE

    @property
    def is_proxy_failure(self) -> bool:
        return self.phase in PROXY_PHASES


@dataclass
class _State:
    tunnel_reached: bool = False


class HttpClient:
    """Executes redirect-following request chains with pooled sessions."""

    def __init__(self, settings):
        self.s = settings
        self._ssl_cache: Dict[bool, ssl.SSLContext] = {}
        self._plain_sessions: Dict[bool, aiohttp.ClientSession] = {}
        self._socks_sessions: Dict[str, aiohttp.ClientSession] = {}
        self._socks_connectors: Dict[str, "ProxyConnector"] = {}

    def _ssl_ctx(self) -> ssl.SSLContext:
        key = self.s.verify_tls
        ctx = self._ssl_cache.get(key)
        if ctx is None:
            ctx = ssl.create_default_context()
            if not key:
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
            self._ssl_cache[key] = ctx
        return ctx

    def _plain_session(self) -> aiohttp.ClientSession:
        key = self.s.verify_tls
        session = self._plain_sessions.get(key)
        if session is None or session.closed:
            connector = TCPConnector(ssl=self._ssl_ctx(), limit=0, ttl_dns_cache=300)
            session = aiohttp.ClientSession(
                connector=connector,
                timeout=ClientTimeout(
                    total=self.s.request_timeout, connect=self.s.connect_timeout
                ),
                headers={"User-Agent": self.s.user_agent},
                trust_env=False,
            )
            self._plain_sessions[key] = session
        return session

    def _proxy_arg(self, spec: Optional[ProxySpec]) -> Optional[str]:
        if spec is None:
            return None
        # never fall back to a direct connection: an assigned proxy is always
        # used, even if its scheme had to be guessed (HTTP-proxy semantics)
        auth = ""
        if spec.username:
            from urllib.parse import quote

            auth = quote(spec.username, safe="")
            if spec.password:
                auth += ":" + quote(spec.password, safe="")
            auth += "@"
        return f"http://{auth}{spec.host}:{spec.port}"

    def _socks_session(self, spec: ProxySpec) -> aiohttp.ClientSession:
        session = self._socks_sessions.get(spec.key)
        if session is not None and not session.closed:
            return session
        ptype = {
            "socks5": ProxyType.SOCKS5,
            "socks5h": ProxyType.SOCKS5,
            "socks4": ProxyType.SOCKS4,
            "socks4a": ProxyType.SOCKS4,
        }[spec.scheme]
        connector = ProxyConnector(
            proxy_type=ptype,
            host=spec.host,
            port=spec.port,
            username=spec.username,
            password=spec.password,
            rdns=spec.scheme in ("socks5h", "socks4a"),
            ssl=self._ssl_ctx(),
            limit=0,
        )
        session = aiohttp.ClientSession(
            connector=connector,
            timeout=ClientTimeout(
                total=self.s.request_timeout, connect=self.s.connect_timeout
            ),
            headers={"User-Agent": self.s.user_agent},
            trust_env=False,
        )
        self._socks_sessions[spec.key] = session
        self._socks_connectors[spec.key] = connector
        return session

    async def close(self) -> None:
        for session in list(self._plain_sessions.values()) + list(
            self._socks_sessions.values()
        ):
            try:
                await session.close()
            except Exception:
                pass
        for connector in self._socks_connectors.values():
            try:
                await connector.close()
            except Exception:
                pass
        self._plain_sessions.clear()
        self._socks_sessions.clear()
        self._socks_connectors.clear()

    async def request_chain(
        self, url: str, spec: Optional[ProxySpec], method: str
    ) -> Outcome:
        """One redirect-following chain. Never raises; returns Outcome."""
        start = time.monotonic()
        state = _State()
        try:
            if spec is not None and spec.is_socks and _HAVE_SOCKS:
                session = self._socks_session(spec)
                proxy_arg = None
            else:
                session = self._plain_session()
                proxy_arg = self._proxy_arg(spec)
            return await self._chain(session, url, method, proxy_arg, state)
        except asyncio.TimeoutError:
            phase = (
                PH_PROXY_TIMEOUT
                if (spec is not None and not state.tunnel_reached)
                else PH_DEST_TIMEOUT
            )
            return Outcome(
                phase=phase, detail="timeout", elapsed=time.monotonic() - start
            )
        except aiohttp.ClientProxyConnectionError:
            return Outcome(
                phase=PH_PROXY_UNREACHABLE,
                detail="cannot connect to proxy",
                elapsed=time.monotonic() - start,
            )
        except aiohttp.ClientHttpProxyError as exc:
            code = getattr(exc, "status", 0)
            if code in (401, 403, 407):
                return Outcome(
                    phase=PH_PROXY_AUTH,
                    detail=f"proxy auth rejected ({code})",
                    elapsed=time.monotonic() - start,
                )
            return Outcome(
                phase=PH_PROXY_CONNECT,
                detail=f"proxy tunnel failure ({code})",
                elapsed=time.monotonic() - start,
            )
        except aiohttp.ClientSSLError as exc:
            return Outcome(
                phase=PH_DEST_TLS,
                detail=f"TLS failure: {exc}",
                elapsed=time.monotonic() - start,
            )
        except aiohttp.ClientConnectorError as exc:
            oserr = getattr(exc, "os_error", None)
            en = getattr(oserr, "errno", None)
            msg = str(oserr or exc)
            low = msg.lower()
            if (
                "name or service not known" in low
                or "nodename nor servname" in low
                or "getaddrinfo failed" in low
            ):
                phase = PH_DEST_DNS_TEMP
                if self._looks_nxdomain(low):
                    phase = PH_DEST_DNS_NXDOMAIN
                return Outcome(phase=phase, detail=msg, elapsed=time.monotonic() - start)
            if en in (111, 10061) or "connection refused" in low or "actively refused" in low:
                if spec is None or state.tunnel_reached:
                    return Outcome(
                        phase=PH_DEST_REFUSED,
                        detail="destination refused connection",
                        elapsed=time.monotonic() - start,
                    )
                return Outcome(
                    phase=PH_PROXY_UNREACHABLE,
                    detail="proxy refused connection",
                    elapsed=time.monotonic() - start,
                )
            if spec is not None and not state.tunnel_reached:
                return Outcome(
                    phase=PH_PROXY_UNREACHABLE,
                    detail=msg,
                    elapsed=time.monotonic() - start,
                )
            return Outcome(
                phase=PH_DEST_REFUSED, detail=msg, elapsed=time.monotonic() - start
            )
        except aiohttp.ClientOSError as exc:
            msg = str(exc).lower()
            if (
                "reset by peer" in msg
                or "broken pipe" in msg
                or "connection aborted" in msg
            ):
                phase = (
                    PH_DEST_RESET
                    if (spec is None or state.tunnel_reached)
                    else PH_AMBIGUOUS
                )
                return Outcome(
                    phase=phase, detail="connection reset", elapsed=time.monotonic() - start
                )
            if "certificate" in msg or "ssl" in msg or "tls" in msg:
                return Outcome(
                    phase=PH_DEST_TLS, detail=str(exc), elapsed=time.monotonic() - start
                )
            phase = PH_AMBIGUOUS if spec is not None else PH_DEST_RESET
            return Outcome(phase=phase, detail=str(exc), elapsed=time.monotonic() - start)
        except aiohttp.TooManyRedirects:
            return Outcome(
                phase=PH_DEST_RESPONSE,
                detail="excessive redirect chain or loop",
                elapsed=time.monotonic() - start,
            )
        except aiohttp.ClientResponseError as exc:
            if exc.status == 407 and spec is not None:
                return Outcome(
                    phase=PH_PROXY_AUTH,
                    detail="proxy auth rejected (407)",
                    elapsed=time.monotonic() - start,
                )
            return Outcome(
                phase=PH_DEST_RESPONSE,
                status_code=exc.status,
                detail=str(exc),
                elapsed=time.monotonic() - start,
            )
        except aiohttp.ClientError as exc:
            phase = PH_AMBIGUOUS if spec is not None else PH_DEST_RESET
            return Outcome(
                phase=phase, detail=str(exc), elapsed=time.monotonic() - start
            )
        except Exception as exc:  # defensive
            return Outcome(
                phase=PH_AMBIGUOUS,
                detail=f"unexpected: {type(exc).__name__}: {exc}",
                elapsed=time.monotonic() - start,
            )

    @staticmethod
    def _looks_nxdomain(msg: str) -> bool:
        return "name or service not known" in msg or "nodename nor servname" in msg

    async def _chain(
        self,
        session: aiohttp.ClientSession,
        url: str,
        method: str,
        proxy_arg: Optional[str],
        state: _State,
    ) -> Outcome:
        history: List[int] = []
        current = url
        redirects = 0
        while True:
            resp = await session.request(
                method,
                current,
                proxy=proxy_arg,
                allow_redirects=False,
                auto_decompress=True,
            )
            if resp.status in (301, 302, 303, 307, 308):
                location = resp.headers.get("Location", "")
                code = resp.status
                resp.release()
                if not location:
                    return Outcome(
                        phase=PH_DEST_RESPONSE,
                        status_code=code,
                        final_url=current,
                        history=history + [code],
                        detail="redirect without Location header",
                        redirects=redirects,
                    )
                nxt = _resolve(current, location)
                redirects += 1
                if redirects > self.s.max_redirects:
                    return Outcome(
                        phase=PH_DEST_RESPONSE,
                        detail="excessive redirect chain",
                        redirects=redirects,
                        history=history,
                    )
                if nxt == current:
                    return Outcome(
                        phase=PH_DEST_RESPONSE,
                        detail="redirect loop",
                        redirects=redirects,
                        history=history,
                    )
                history.append(code)
                current = nxt
                continue
            # terminal response: this is destination evidence
            state.tunnel_reached = True
            headers = {k.lower(): v for k, v in resp.headers.items()}
            chunks: list = []
            size = 0
            try:
                async for chunk in resp.content.iter_chunked(16384):
                    chunks.append(chunk)
                    size += len(chunk)
                    if size >= self.s.max_body_bytes:
                        break
            except Exception:
                pass  # partial body still usable for classification
            body = b"".join(chunks)[: self.s.max_body_bytes] if chunks else b""
            code = resp.status
            resp.release()
            return Outcome(
                phase=PH_DEST_RESPONSE,
                status_code=code,
                final_url=current,
                history=history + [code],
                headers=headers,
                body=body if method == "GET" else None,
                redirects=redirects,
            )
