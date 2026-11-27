"""Advanced-mode fingerprint database.

Fingerprints are matched contextually with confidence scoring. A single body
keyword is never enough: classification requires either a high-weight title
match or multiple corroborating signals. JavaScript-heavy and text-rich pages
raise the required evidence, so legitimate articles that merely mention
"page not found" are not rejected.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Tuple

# scopes
TITLE = "title"
BODY = "body"
BOTH = "both"

# kinds
K_PARKED = "parked"
K_HOSTING_DEFAULT = "hosting_default"
K_SOFT404 = "soft404"
K_CDN_ERROR = "cdn_error"
K_BROWSER_ERROR = "browser_error"
K_SUSPENDED = "suspended"
K_JS_FRAMEWORK = "js_framework"
K_TEXT_RICH = "text_rich"


@dataclass(frozen=True)
class Fingerprint:
    pattern: str
    kind: str
    weight: int
    scope: str = BOTH  # title | body | both
    exact_title: bool = False


# --- parked domains & marketplaces -----------------------------------------
PARKED: List[Fingerprint] = [
    Fingerprint("buy this domain", K_PARKED, 60, TITLE),
    Fingerprint("domain is for sale", K_PARKED, 60, BOTH),
    Fingerprint("domain for sale", K_PARKED, 55, BOTH),
    Fingerprint("this domain is for sale", K_PARKED, 65, BOTH),
    Fingerprint("buy this domain", K_PARKED, 45, BODY),
    Fingerprint("this web page is parked", K_PARKED, 65, BOTH),
    Fingerprint("domain parking", K_PARKED, 40, BODY),
    Fingerprint("parked free, courtesy of", K_PARKED, 65, BODY),
    Fingerprint("sedoparking", K_PARKED, 60, BOTH),
    Fingerprint("sedo domain parking", K_PARKED, 60, BODY),
    Fingerprint("bodis.com", K_PARKED, 55, BODY),
    Fingerprint("parkingcrew", K_PARKED, 55, BODY),
    Fingerprint("hugedomains", K_PARKED, 55, BOTH),
    Fingerprint("hugedomains.com", K_PARKED, 60, BODY),
    Fingerprint("dan.com", K_PARKED, 30, BODY),
    Fingerprint("afternic", K_PARKED, 45, BODY),
    Fingerprint("namebright", K_PARKED, 45, BODY),
    Fingerprint("domain may be for sale", K_PARKED, 35, BODY),
    Fingerprint("related searches", K_PARKED, 15, BODY),
    Fingerprint("sponsored listings", K_PARKED, 20, BODY),
    Fingerprint("this domain name is for sale", K_PARKED, 60, BOTH),
    Fingerprint("the domain has been registered", K_PARKED, 30, BODY),
]

# --- hosting provider defaults ----------------------------------------------
HOSTING_DEFAULT: List[Fingerprint] = [
    Fingerprint("welcome to nginx!", K_HOSTING_DEFAULT, 65, TITLE),
    Fingerprint("welcome to nginx", K_HOSTING_DEFAULT, 60, BOTH),
    Fingerprint("apache2 ubuntu default page", K_HOSTING_DEFAULT, 65, TITLE, exact_title=True),
    Fingerprint("it works!", K_HOSTING_DEFAULT, 40, BOTH),
    Fingerprint("this is the default index.html", K_HOSTING_DEFAULT, 55, BODY),
    Fingerprint("welcome to your new website", K_HOSTING_DEFAULT, 45, BOTH),
    Fingerprint("future home of something quite cool", K_HOSTING_DEFAULT, 65, TITLE),
    Fingerprint("is under construction", K_HOSTING_DEFAULT, 30, BOTH),
    Fingerprint("site not published", K_HOSTING_DEFAULT, 60, BOTH),
    Fingerprint("there is no website configured at this address", K_HOSTING_DEFAULT, 65, BOTH),
    Fingerprint("default web site page", K_HOSTING_DEFAULT, 60, BOTH),
    Fingerprint("welcome to your new site", K_HOSTING_DEFAULT, 45, TITLE),
    Fingerprint("this site can’t be found", K_HOSTING_DEFAULT, 40, BODY),
    Fingerprint("your connection is not private", K_HOSTING_DEFAULT, 10, BODY),
    Fingerprint("quick website builder", K_HOSTING_DEFAULT, 20, BODY),
    Fingerprint("coming soon", K_HOSTING_DEFAULT, 15, BOTH),
    Fingerprint("index of /", K_HOSTING_DEFAULT, 40, TITLE, exact_title=True),
    Fingerprint("directory listing for", K_HOSTING_DEFAULT, 50, BOTH),
    Fingerprint("under construction", K_HOSTING_DEFAULT, 30, BOTH),
    Fingerprint("website coming soon", K_HOSTING_DEFAULT, 40, BOTH),
    Fingerprint("default page", K_HOSTING_DEFAULT, 15, BOTH),
    Fingerprint("hosting resumed", K_HOSTING_DEFAULT, 25, BODY),
]

# --- soft 404 ---------------------------------------------------------------
SOFT404: List[Fingerprint] = [
    Fingerprint("404", K_SOFT404, 25, TITLE),
    Fingerprint("page not found", K_SOFT404, 30, TITLE),
    Fingerprint("not found", K_SOFT404, 20, TITLE),
    Fingerprint("error 404", K_SOFT404, 40, BOTH),
    Fingerprint("http 404", K_SOFT404, 40, BOTH),
    Fingerprint("404 not found", K_SOFT404, 45, BOTH),
    Fingerprint("404 error", K_SOFT404, 40, BOTH),
    Fingerprint("oops, that page", K_SOFT404, 45, BOTH),
    Fingerprint("the page you requested was not found", K_SOFT404, 50, BODY),
    Fingerprint("page cannot be found", K_SOFT404, 40, BOTH),
    Fingerprint("couldn’t find that page", K_SOFT404, 40, BOTH),
    Fingerprint("could not be found", K_SOFT404, 20, BODY),
    Fingerprint("doesn’t exist", K_SOFT404, 25, BODY),
    Fingerprint("does not exist", K_SOFT404, 20, BODY),
    Fingerprint("this page doesn't exist", K_SOFT404, 45, BOTH),
    Fingerprint("this page does not exist", K_SOFT404, 45, BOTH),
    Fingerprint("you may have mistyped the address", K_SOFT404, 45, BODY),
    Fingerprint("the requested url was not found", K_SOFT404, 50, BODY),
    Fingerprint("nothing to see here", K_SOFT404, 25, BODY),
    Fingerprint("go back to homepage", K_SOFT404, 10, BODY),
    Fingerprint("return to home", K_SOFT404, 8, BODY),
]

# --- CDN / server error pages served with 2xx -------------------------------
CDN_ERROR: List[Fingerprint] = [
    Fingerprint("attention required! | cloudflare", K_CDN_ERROR, 65, TITLE),
    Fingerprint("checking your browser before accessing", K_CDN_ERROR, 30, BODY),
    Fingerprint("please stand by, while we are checking your browser", K_CDN_ERROR, 30, BODY),
    Fingerprint("ddos protection by cloudflare", K_CDN_ERROR, 25, BODY),
    Fingerprint("error 1000", K_CDN_ERROR, 40, BODY),
    Fingerprint("error 1001", K_CDN_ERROR, 40, BODY),
    Fingerprint("dns points to prohibited ip", K_CDN_ERROR, 55, BODY),
    Fingerprint("fastly error: unknown domain", K_CDN_ERROR, 60, BODY),
    Fingerprint("502 bad gateway", K_CDN_ERROR, 45, BOTH),
    Fingerprint("503 service unavailable", K_CDN_ERROR, 45, BOTH),
    Fingerprint("504 gateway time-out", K_CDN_ERROR, 45, BOTH),
    Fingerprint("gateway timeout", K_CDN_ERROR, 35, BOTH),
    Fingerprint("bad gateway", K_CDN_ERROR, 25, BODY),
    Fingerprint("service unavailable", K_CDN_ERROR, 20, BODY),
    Fingerprint("internal server error", K_CDN_ERROR, 25, BOTH),
    Fingerprint("application error", K_CDN_ERROR, 15, BODY),
    Fingerprint("heroku | no such app", K_CDN_ERROR, 60, BOTH),
    Fingerprint("no such app", K_CDN_ERROR, 40, BODY),
    Fingerprint("there isn't a github pages site here", K_CDN_ERROR, 60, BOTH),
    Fingerprint("404 - page not found", K_CDN_ERROR, 40, BOTH),
]

# --- browser-generated error pages ------------------------------------------
BROWSER_ERROR: List[Fingerprint] = [
    Fingerprint("this site can’t be reached", K_BROWSER_ERROR, 60, BOTH),
    Fingerprint("this site can't be reached", K_BROWSER_ERROR, 60, BOTH),
    Fingerprint("err_name_not_resolved", K_BROWSER_ERROR, 60, BOTH),
    Fingerprint("err_connection_refused", K_BROWSER_ERROR, 55, BOTH),
    Fingerprint("err_connection_timed_out", K_BROWSER_ERROR, 55, BOTH),
    Fingerprint("err_connection_reset", K_BROWSER_ERROR, 55, BOTH),
    Fingerprint("err_too_many_redirects", K_BROWSER_ERROR, 55, BOTH),
    Fingerprint("this page isn’t working", K_BROWSER_ERROR, 45, BOTH),
    Fingerprint("this page isn't working", K_BROWSER_ERROR, 45, BOTH),
    Fingerprint("hmmm… can't reach this page", K_BROWSER_ERROR, 55, BOTH),
    Fingerprint("can't reach this page", K_BROWSER_ERROR, 45, BOTH),
    Fingerprint("took too long to respond", K_BROWSER_ERROR, 40, BODY),
    Fingerprint("dns_probe_finished_nxdomain", K_BROWSER_ERROR, 55, BOTH),
    Fingerprint("server ip address could not be found", K_BROWSER_ERROR, 50, BOTH),
]

# --- suspended / disconnected -----------------------------------------------
SUSPENDED: List[Fingerprint] = [
    Fingerprint("account suspended", K_SUSPENDED, 60, TITLE),
    Fingerprint("this account has been suspended", K_SUSPENDED, 65, BOTH),
    Fingerprint("website suspended", K_SUSPENDED, 60, BOTH),
    Fingerprint("account suspended by", K_SUSPENDED, 55, BODY),
    Fingerprint("site temporarily unavailable", K_SUSPENDED, 30, BODY),
    Fingerprint("this website has been suspended", K_SUSPENDED, 65, BOTH),
    Fingerprint("domain has been suspended", K_SUSPENDED, 60, BOTH),
    Fingerprint("suspended domain", K_SUSPENDED, 55, BOTH),
    Fingerprint("account has been suspended", K_SUSPENDED, 60, BODY),
    Fingerprint("server suspended", K_SUSPENDED, 40, BODY),
    Fingerprint("upgraded hosting plan required", K_SUSPENDED, 35, BODY),
    Fingerprint("000webhost", K_SUSPENDED, 20, BODY),
    Fingerprint("bannerfarm.000webhost", K_SUSPENDED, 20, BODY),
]

# --- evidence of a real, live site ------------------------------------------
JS_FRAMEWORK: List[Fingerprint] = [
    Fingerprint("__next_data__", K_JS_FRAMEWORK, 0),
    Fingerprint('id="root"', K_JS_FRAMEWORK, 0),
    Fingerprint('id="app"', K_JS_FRAMEWORK, 0),
    Fingerprint("nuxt", K_JS_FRAMEWORK, 0),
    Fingerprint("react", K_JS_FRAMEWORK, 0),
    Fingerprint("vue.js", K_JS_FRAMEWORK, 0),
    Fingerprint("angular", K_JS_FRAMEWORK, 0),
    Fingerprint("svelte", K_JS_FRAMEWORK, 0),
]

_ALL: List[Fingerprint] = (
    PARKED + HOSTING_DEFAULT + SOFT404 + CDN_ERROR + BROWSER_ERROR + SUSPENDED
)

# compile once (cache)
_COMPILED: List[Tuple[Fingerprint, str]] = [(fp, fp.pattern.lower()) for fp in _ALL]

_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_SCRIPT_RE = re.compile(r"<script\b.*?</script>", re.IGNORECASE | re.DOTALL)
_STYLE_RE = re.compile(r"<style\b.*?</style>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_ENTITY_RE = re.compile(r"&[a-z#0-9]+;")
_WS_RE = re.compile(r"\s+")
_ENTITY_MAP = {
    "&amp;": "&",
    "&lt;": "<",
    "&gt;": ">",
    "&quot;": '"',
    "&#39;": "'",
    "&nbsp;": " ",
    "&ndash;": "-",
    "&mdash;": "—",
    "&rsquo;": "’",
    "&lsquo;": "‘",
    "&hellip;": "…",
}


def extract_title(html: str) -> str:
    m = _TITLE_RE.search(html)
    if not m:
        return ""
    title = m.group(1)
    for k, v in _ENTITY_MAP.items():
        title = title.replace(k, v)
    return _WS_RE.sub(" ", title).strip()


def extract_visible_text(html: str, limit: int = 20000) -> str:
    html = _SCRIPT_RE.sub(" ", html)
    html = _STYLE_RE.sub(" ", html)
    text = _TAG_RE.sub(" ", html)
    for k, v in _ENTITY_MAP.items():
        text = text.replace(k, v)
    text = _WS_RE.sub(" ", text).strip()
    return text[:limit]


def _count_in(scope_text: str, needle: str) -> int:
    if not needle:
        return 0
    return scope_text.count(needle)


@dataclass
class ContentSignals:
    html: str
    title: str = ""
    text: str = ""
    text_len: int = 0
    link_count: int = 0
    js_markers: int = 0


def analyze_content(html: str) -> ContentSignals:
    title = extract_title(html)
    text = extract_visible_text(html)
    links = html.lower().count("<a ")
    js_markers = sum(
        1 for fp in JS_FRAMEWORK if fp.pattern in html.lower()
    )
    return ContentSignals(
        html=html,
        title=title,
        text=text,
        text_len=len(text),
        link_count=links,
        js_markers=js_markers,
    )


@dataclass
class FingerprintScore:
    kind: str
    score: int
    hits: List[str] = field(default_factory=list)
    title_hit: bool = False


def score_html(signals: ContentSignals) -> Dict[str, FingerprintScore]:
    title_l = signals.title.lower()
    text_l = signals.text.lower()
    scores: Dict[str, FingerprintScore] = {}

    def add(kind: str, score: int, hit: str, title_hit: bool) -> None:
        entry = scores.setdefault(kind, FingerprintScore(kind, 0))
        entry.score += score
        entry.hits.append(hit)
        entry.title_hit = entry.title_hit or title_hit

    for fp, needle in _COMPILED:
        if fp.scope in (TITLE, BOTH) and title_l and needle in title_l:
            weight = fp.weight + (10 if fp.exact_title and title_l.strip() == needle else 0)
            add(fp.kind, weight, needle, True)
        elif fp.scope in (BODY, BOTH) and needle in text_l:
            # cap repeated pattern contribution
            occurrences = min(_count_in(text_l, needle), 3)
            add(fp.kind, fp.weight * occurrences, needle, False)

    return scores


def looks_text_rich(signals: ContentSignals) -> bool:
    return signals.text_len >= 1200 and signals.link_count >= 5
