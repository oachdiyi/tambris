"""End-to-end HTTP tests against live local servers (direct, no proxy)."""

from __future__ import annotations

import pytest

from sitecheck.checker import Checker
from sitecheck.http_client import HttpClient, PH_DEST_RESPONSE, PH_DEST_TLS
from sitecheck.classify import S_VALID, S_DEAD, S_RESTRICTED, S_ERROR


def make_checker(tmp_path, **overrides) -> Checker:
    from tests.conftest import make_settings

    settings = make_settings(tmp_path, **overrides)
    return Checker(settings, pool=None), settings


async def test_direct_valid(tmp_path, target_server):
    checker, _ = make_checker(tmp_path)
    result = await checker.check(f"{target_server}/ok")
    assert result.category == S_VALID
    assert result.status_code == 200
    assert result.final_url == f"{target_server}/ok"
    assert result.proxy is None


async def test_direct_dead_404(tmp_path, target_server):
    checker, _ = make_checker(tmp_path)
    assert (await checker.check(f"{target_server}/status/404")).category == S_DEAD
    assert (await checker.check(f"{target_server}/status/410")).category == S_DEAD


async def test_direct_restricted(tmp_path, target_server):
    checker, _ = make_checker(tmp_path)
    for code in (401, 403, 429, 451):
        r = await checker.check(f"{target_server}/status/{code}")
        assert r.category == S_RESTRICTED, code


async def test_direct_error_statuses(tmp_path, target_server):
    checker, _ = make_checker(tmp_path)
    for code in (400, 500, 503):
        r = await checker.check(f"{target_server}/status/{code}")
        assert r.category == S_ERROR, code


async def test_redirect_chain_final_url(tmp_path, target_server):
    checker, _ = make_checker(tmp_path)
    result = await checker.check(f"{target_server}/redirect/5")
    assert result.category == S_VALID
    assert result.final_url == f"{target_server}/ok"


async def test_relative_redirect_final_url(tmp_path, target_server):
    checker, _ = make_checker(tmp_path)
    result = await checker.check(f"{target_server}/relative-redirect")
    assert result.category == S_VALID
    assert result.final_url.endswith("/deeper/page")


async def test_redirect_loop_is_error_not_valid(tmp_path, target_server):
    checker, _ = make_checker(tmp_path)
    result = await checker.check(f"{target_server}/loop1")
    assert result.category == S_ERROR
    assert "loop" in result.reason or "redirect" in result.reason


async def test_excessive_redirect_chain(tmp_path, target_server):
    checker, s = make_checker(tmp_path, max_redirects=3)
    result = await checker.check(f"{target_server}/redirect/10")
    assert result.category == S_ERROR


async def test_head_405_get_fallback(tmp_path, target_server):
    checker, _ = make_checker(tmp_path, head_first=True)
    result = await checker.check(f"{target_server}/head-405")
    assert result.category == S_VALID


async def test_head_first_minimizes_requests(tmp_path, target_server):
    checker, _ = make_checker(tmp_path, head_first=True)
    result = await checker.check(f"{target_server}/ok")
    assert result.category == S_VALID
    assert result.status_code == 200


async def test_tls_failure_with_verification(tmp_path, https_server):
    https_url, _ctx = https_server
    checker, _ = make_checker(tmp_path, https_fallback_http=False)
    result = await checker.check(f"{https_url}/ok")
    # self-signed cert must fail verification and must not be a success
    assert result.category == S_ERROR
    assert "tls" in result.reason.lower() or "ssl" in result.reason.lower() or "certificate" in result.reason.lower()


async def test_https_to_http_fallback_on_tls_failure(tmp_path, target_server):
    # TLS ClientHello sent to a plain-HTTP port -> genuine TLS failure,
    # then the configured HTTP fallback must recover the check.
    http_port = target_server.split(":")[-1]
    checker, _ = make_checker(tmp_path, https_fallback_http=True)
    result = await checker.check(f"https://127.0.0.1:{http_port}/ok")
    assert result.category == S_VALID
    assert result.final_url.startswith("http://")
    assert "TLS failed" in result.reason or "HTTP fallback" in result.reason


async def test_tls_failure_never_falls_back_when_disabled(tmp_path, target_server):
    http_port = target_server.split(":")[-1]
    checker, _ = make_checker(tmp_path, https_fallback_http=False)
    result = await checker.check(f"https://127.0.0.1:{http_port}/ok")
    assert result.category == S_ERROR


async def test_dns_failure_classification(tmp_path):
    checker, _ = make_checker(tmp_path, request_timeout=5, connect_timeout=3)
    result = await checker.check("http://this-domain-does-not-exist-9f3x2.invalid/")
    assert result.category in (S_DEAD, S_ERROR)
    assert result.category != S_VALID


async def test_connection_refused(tmp_path):
    checker, _ = make_checker(tmp_path, connect_timeout=2, request_timeout=3)
    result = await checker.check("http://127.0.0.1:1/")
    assert result.category == S_ERROR


async def test_advanced_mode_soft404(tmp_path, target_server):
    checker, _ = make_checker(tmp_path, mode="advanced")
    result = await checker.check(f"{target_server}/soft404")
    assert result.category == "DEAD"


async def test_advanced_mode_parked(tmp_path, target_server):
    checker, _ = make_checker(tmp_path, mode="advanced")
    result = await checker.check(f"{target_server}/parked")
    assert result.category == "PARKED"


async def test_advanced_mode_hosting_default(tmp_path, target_server):
    checker, _ = make_checker(tmp_path, mode="advanced")
    result = await checker.check(f"{target_server}/hosting-default")
    assert result.category == "FAILED"


async def test_advanced_mode_suspended(tmp_path, target_server):
    checker, _ = make_checker(tmp_path, mode="advanced")
    result = await checker.check(f"{target_server}/suspended")
    assert result.category == "DEAD"


async def test_advanced_mode_cdn_error(tmp_path, target_server):
    checker, _ = make_checker(tmp_path, mode="advanced")
    result = await checker.check(f"{target_server}/cdn-error")
    assert result.category == "FAILED"


async def test_advanced_mode_js_heavy_ok(tmp_path, target_server):
    checker, _ = make_checker(tmp_path, mode="advanced")
    result = await checker.check(f"{target_server}/js-heavy")
    assert result.category in ("SUCCESS", "UNKNOWN")
    assert result.category != "FAILED"


async def test_advanced_mode_legit_article(tmp_path, target_server):
    checker, _ = make_checker(tmp_path, mode="advanced")
    result = await checker.check(f"{target_server}/article")
    assert result.category == "SUCCESS"


async def test_advanced_mode_nxdomain(tmp_path):
    checker, _ = make_checker(tmp_path, mode="advanced", connect_timeout=3, request_timeout=5)
    result = await checker.check("http://this-domain-does-not-exist-9f3x2.invalid/")
    assert result.category in ("DEAD", "UNKNOWN", "FAILED")


async def test_timeout_classification(tmp_path, target_server):
    checker, _ = make_checker(tmp_path, request_timeout=0.3, connect_timeout=0.3, head_first=False)
    result = await checker.check(f"{target_server}/slow?t=3")
    assert result.category == S_ERROR


async def test_head_timeout_get_fallback_regression(tmp_path, target_server):
    # regression: HEAD hitting a destination timeout must fall back to GET
    # without raising (PH_DEST_TIMEOUT import bug)
    checker, _ = make_checker(tmp_path, head_first=True, request_timeout=0.3, connect_timeout=0.3)
    result = await checker.check(f"{target_server}/slow?t=3")
    assert result.category in (S_VALID, S_ERROR)
