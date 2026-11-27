"""URL normalization and lightweight validation."""

from __future__ import annotations

import ipaddress
import re
from urllib.parse import urlsplit, urlunsplit, urljoin

_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://")
_BARE_HOST_RE = re.compile(
    r"^(?:[a-zA-Z0-9](?:[a-zA-Z0-9-]*[a-zA-Z0-9])?\.)+[A-Za-z]{2,}$"
)
_CTRL_RE = re.compile(r"[\x00-\x20\x7f]")


class UrlError(ValueError):
    pass


def ensure_scheme(url: str, prefer_https: bool = True) -> str:
    url = url.strip()
    if not _SCHEME_RE.match(url):
        if "://" in url:
            raise UrlError(f"invalid scheme in {url!r}")
        url = ("https://" if prefer_https else "http://") + url
    return url


def normalize_url(raw: str, prefer_https: bool = True) -> str:
    """Normalize a raw input line into a canonical URL string.

    Raises UrlError for lines that cannot be a website URL.
    """
    url = ensure_scheme(raw, prefer_https=prefer_https)
    url = _CTRL_RE.sub("", url)
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise UrlError(f"unsupported scheme {parts.scheme!r}")
    host = parts.hostname
    if not host:
        raise UrlError(f"missing host in {raw!r}")
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    try:
        ipaddress.ip_address(host)
    except ValueError:
        host = host.rstrip(".").lower()
        if not _BARE_HOST_RE.match(host) and not host.endswith("localhost"):
            raise UrlError(f"invalid host {host!r}")
    netloc = host
    if parts.port is not None:
        default = (parts.scheme == "https" and parts.port == 443) or (
            parts.scheme == "http" and parts.port == 80
        )
        if not default:
            netloc = f"{host}:{parts.port}"
    path = parts.path or "/"
    normalized = urlunsplit((parts.scheme, netloc, path, parts.query, ""))
    return normalized


def root_of(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, "/", "", ""))


def resolve(base: str, location: str) -> str:
    return urljoin(base, location)
