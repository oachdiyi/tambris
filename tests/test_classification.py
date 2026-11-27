"""Unit tests: URL normalization, simple classification, advanced scoring."""

from __future__ import annotations

import pytest

from sitecheck.urlproc import normalize_url, UrlError, root_of
from sitecheck.classify import classify_simple
from sitecheck.http_client import (
    Outcome,
    PH_DEST_RESPONSE,
    PH_DEST_DNS_NXDOMAIN,
    PH_DEST_DNS_TEMP,
    PH_DEST_TLS,
    PH_DEST_TIMEOUT,
    PH_DEST_REFUSED,
)
from sitecheck.advanced import (
    classify_advanced,
    A_SUCCESS,
    A_DEAD,
    A_RESTRICTED,
    A_PARKED,
    A_FAILED,
    A_UNKNOWN,
)


# ----------------------------------------------------------------- urls

def test_normalize_adds_https():
    assert normalize_url("example.com").startswith("https://example.com")


def test_normalize_keeps_http():
    assert normalize_url("http://example.com/path") == "http://example.com/path"


def test_normalize_strips_whitespace_and_controls():
    assert normalize_url("  example.com/path\x00 ") == "https://example.com/path"


def test_normalize_lowercases_host():
    assert normalize_url("ExAmPlE.COM/Path") == "https://example.com/Path"


def test_normalize_default_port_removed():
    assert normalize_url("https://example.com:443/") == "https://example.com/"
    assert normalize_url("http://example.com:8080/") == "http://example.com:8080/"


def test_normalize_rejects_garbage():
    with pytest.raises(UrlError):
        normalize_url("not a url !!")
    with pytest.raises(UrlError):
        normalize_url("ftp://example.com")


def test_root_of():
    assert root_of("https://example.com/a/b?c=1") == "https://example.com/"


# ------------------------------------------------------- simple mode

def test_simple_valid_2xx():
    assert classify_simple(Outcome(phase=PH_DEST_RESPONSE, status_code=200)) == ("VALID", "HTTP 200")
    assert classify_simple(Outcome(phase=PH_DEST_RESPONSE, status_code=204))[0] == "VALID"


def test_simple_redirect_chain_is_valid():
    assert classify_simple(
        Outcome(phase=PH_DEST_RESPONSE, status_code=200, history=[301, 302])
    )[0] == "VALID"


def test_simple_dead():
    assert classify_simple(Outcome(phase=PH_DEST_RESPONSE, status_code=404))[0] == "DEAD"
    assert classify_simple(Outcome(phase=PH_DEST_RESPONSE, status_code=410))[0] == "DEAD"


def test_simple_nxdomain_dead_temp_dns_error():
    assert classify_simple(Outcome(phase=PH_DEST_DNS_NXDOMAIN))[0] == "DEAD"
    assert classify_simple(Outcome(phase=PH_DEST_DNS_TEMP))[0] == "ERROR"


def test_simple_restricted():
    for code in (401, 403, 405, 429, 451):
        assert classify_simple(Outcome(phase=PH_DEST_RESPONSE, status_code=code))[0] == "RESTRICTED"


def test_simple_error():
    assert classify_simple(Outcome(phase=PH_DEST_RESPONSE, status_code=400))[0] == "ERROR"
    assert classify_simple(Outcome(phase=PH_DEST_RESPONSE, status_code=500))[0] == "ERROR"
    assert classify_simple(Outcome(phase=PH_DEST_TLS))[0] == "ERROR"
    assert classify_simple(Outcome(phase=PH_DEST_REFUSED))[0] == "ERROR"
    assert classify_simple(Outcome(phase=PH_DEST_TIMEOUT))[0] == "ERROR"


# ------------------------------------------------------ advanced mode

def _html_outcome(body: str, code: int = 200, headers=None) -> Outcome:
    return Outcome(
        phase=PH_DEST_RESPONSE,
        status_code=code,
        final_url="https://x.test/",
        body=body.encode(),
        headers=headers or {},
    )


def test_advanced_real_site_success():
    body = (
        "<html><head><title>My Blog</title></head><body>"
        "<p>" + "Interesting article content about technology. " * 100 + "</p>"
        "<a href='/1'>a</a><a href='/2'>b</a><a href='/3'>c</a>"
        "<a href='/4'>d</a><a href='/5'>e</a></body></html>"
    )
    assert classify_advanced(_html_outcome(body)).category == A_SUCCESS


def test_advanced_article_mentioning_page_not_found_is_success():
    body = (
        "<html><head><title>How to fix page not found errors</title></head><body>"
        "<p>" + "A thorough guide about fixing 404 issues on your own website. " * 80 +
        "</p><a href='/1'>a</a><a href='/2'>b</a><a href='/3'>c</a>"
        "<a href='/4'>d</a><a href='/5'>e</a></body></html>"
    )
    assert classify_advanced(_html_outcome(body)).category == A_SUCCESS


def test_advanced_soft404_detected():
    body = (
        "<html><head><title>404 Not Found</title></head><body>"
        "<h1>Error 404 - page not found</h1>"
        "<p>The requested url was not found.</p></body></html>"
    )
    assert classify_advanced(_html_outcome(body)).category == A_DEAD


def test_advanced_404_status_is_dead():
    assert classify_advanced(_html_outcome("x", code=404)).category == A_DEAD


def test_advanced_parked_detected():
    body = (
        "<html><head><title>Buy this domain</title></head><body>"
        "<h1>This domain is for sale</h1><p>Sponsored listings. Related searches.</p>"
        "</body></html>"
    )
    assert classify_advanced(_html_outcome(body)).category == A_PARKED


def test_advanced_hosting_default_failed():
    body = (
        "<html><head><title>Welcome to nginx!</title></head><body>"
        "<h1>Welcome to nginx!</h1><p>If you see this page the web server is "
        "installed and working.</p></body></html>"
    )
    assert classify_advanced(_html_outcome(body)).category == A_FAILED


def test_advanced_suspended_dead():
    body = (
        "<html><head><title>Account Suspended</title></head><body>"
        "<h1>This account has been suspended</h1></body></html>"
    )
    assert classify_advanced(_html_outcome(body)).category == A_DEAD


def test_advanced_browser_error_page_failed():
    body = (
        "<html><head><title>This site can't be reached</title></head><body>"
        "<p>ERR_CONNECTION_REFUSED</p></body></html>"
    )
    assert classify_advanced(_html_outcome(body)).category == A_FAILED


def test_advanced_js_heavy_site_not_false_positive():
    body = (
        "<html><head><title>SPA App</title></head><body><div id=\"root\"></div>"
        "<script>window.__NEXT_DATA__={}</script></body></html>"
    )
    verdict = classify_advanced(_html_outcome(body))
    assert verdict.category in (A_SUCCESS, A_UNKNOWN)
    assert verdict.category != A_FAILED


def test_advanced_minimal_html_is_unknown_not_success():
    verdict = classify_advanced(_html_outcome("<html><body>hi</body></html>"))
    assert verdict.category == A_UNKNOWN


def test_advanced_empty_body_200_is_unknown():
    assert classify_advanced(_html_outcome("")).category == A_UNKNOWN


def test_advanced_restricted():
    assert classify_advanced(_html_outcome("x", code=403)).category == A_RESTRICTED
    assert classify_advanced(_html_outcome("x", code=429)).category == A_RESTRICTED


def test_advanced_nxdomain_dead_temp_dns_unknown():
    o = Outcome(phase=PH_DEST_DNS_NXDOMAIN)
    assert classify_advanced(o).category == A_DEAD
    o = Outcome(phase=PH_DEST_DNS_TEMP)
    assert classify_advanced(o).category == A_UNKNOWN


def test_advanced_5xx_failed():
    assert classify_advanced(_html_outcome("x", code=503)).category == A_FAILED
