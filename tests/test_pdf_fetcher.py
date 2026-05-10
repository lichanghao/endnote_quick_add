from __future__ import annotations

from pathlib import Path

import pytest
import requests_mock

from endnote_quick_add import pdf_fetcher
from endnote_quick_add.pdf_fetcher import (
    CookieLoadError,
    _extract_arxiv_id,
    _http_get,
    _load_browser_cookies,
    _registered_domain,
    fetch_pdf,
    fetch_pdf_with_handoff,
    use_local_pdf,
)
from endnote_quick_add.resolver import CrossRefRecord


PDF_BYTES = b"%PDF-1.4\n" + b"x" * 200 + b"\n%%EOF\n"


def make_record(**overrides) -> CrossRefRecord:
    base = dict(
        doi="10.1038/test",
        title="A Test Paper",
        authors=["Ada Lovelace"],
        year="2024",
        container_title="Journal of Tests",
        volume="12",
        issue="3",
        page="100-110",
        publisher="Nature",
        type="journal-article",
        url="https://example.com/paper",
        raw={},
    )
    base.update(overrides)
    return CrossRefRecord(**base)


def test_extract_arxiv_id_from_doi():
    rec = make_record(doi="10.48550/arXiv.1706.03762")
    assert _extract_arxiv_id(rec) == "1706.03762"


def test_extract_arxiv_id_from_relation():
    rec = make_record(
        raw={"relation": {"has-preprint": [{"id": "10.48550/arXiv.2103.00020"}]}}
    )
    assert _extract_arxiv_id(rec) == "2103.00020"


def test_extract_arxiv_id_returns_none_without_signal():
    assert _extract_arxiv_id(make_record()) is None


def test_arxiv_short_circuits_other_sources(tmp_path: Path):
    rec = make_record(doi="10.48550/arXiv.1706.03762")
    with requests_mock.Mocker() as m:
        m.get("https://arxiv.org/pdf/1706.03762.pdf", content=PDF_BYTES)
        # If we tried unpaywall or publisher, those calls would be unmatched.
        result, log = fetch_pdf(
            rec,
            cache_dir=tmp_path,
            unpaywall_email="x@y.com",
            scihub_mirror="https://sci-hub.example",
        )
    assert result is not None
    assert result.source == "arxiv"
    assert result.pdf_path.read_bytes().startswith(b"%PDF")
    assert log == ["arxiv: ok"]


def test_falls_through_to_unpaywall_when_arxiv_missing(tmp_path: Path):
    rec = make_record()
    with requests_mock.Mocker() as m:
        m.get(
            "https://api.unpaywall.org/v2/10.1038/test",
            json={
                "best_oa_location": {"url_for_pdf": "https://oa.example/paper.pdf"},
            },
        )
        m.get("https://oa.example/paper.pdf", content=PDF_BYTES)
        result, log = fetch_pdf(
            rec,
            cache_dir=tmp_path,
            unpaywall_email="x@y.com",
            scihub_mirror=None,
        )
    assert result is not None
    assert result.source == "unpaywall"
    # arxiv was attempted and failed (no arxiv id), then unpaywall succeeded.
    assert log[0].startswith("arxiv: failed")
    assert log[-1] == "unpaywall: ok"


def test_publisher_extracts_citation_pdf_url(tmp_path: Path):
    rec = make_record()
    landing_html = """
    <html><head>
      <meta name="citation_pdf_url" content="https://example.com/full.pdf">
    </head></html>
    """
    with requests_mock.Mocker() as m:
        # No arxiv id in record → arxiv source fails internally.
        m.get(
            "https://api.unpaywall.org/v2/10.1038/test",
            json={"best_oa_location": None},
        )
        m.get("https://example.com/paper", text=landing_html, headers={"Content-Type": "text/html"})
        m.get("https://example.com/full.pdf", content=PDF_BYTES)
        result, log = fetch_pdf(
            rec,
            cache_dir=tmp_path,
            unpaywall_email="x@y.com",
            scihub_mirror=None,
        )
    assert result is not None
    assert result.source == "publisher"
    assert "publisher: ok" in log


def test_publisher_reports_cloudflare_challenge(tmp_path: Path):
    rec = make_record(url="https://journals.aps.org/prl/abstract/10.1103/lk32-njx7")
    with requests_mock.Mocker() as m:
        m.get(
            "https://journals.aps.org/prl/abstract/10.1103/lk32-njx7",
            status_code=403,
            headers={"cf-mitigated": "challenge"},
            text="<html>challenge</html>",
        )
        result, log = fetch_pdf(
            rec,
            cache_dir=tmp_path,
            unpaywall_email=None,
            scihub_mirror=None,
        )
    assert result is None
    assert any("Cloudflare challenge" in line for line in log)


def test_publisher_cloudflare_challenge_returns_browser_handoff(tmp_path: Path):
    rec = make_record(url="https://journals.aps.org/prl/abstract/10.1103/lk32-njx7")
    with requests_mock.Mocker() as m:
        m.get(
            "https://journals.aps.org/prl/abstract/10.1103/lk32-njx7",
            status_code=403,
            headers={"cf-mitigated": "challenge"},
            text="<html>challenge</html>",
        )
        result, log, handoff = fetch_pdf_with_handoff(
            rec,
            cache_dir=tmp_path,
            unpaywall_email=None,
            scihub_mirror=None,
        )
    assert result is None
    assert any("Cloudflare challenge" in line for line in log)
    assert handoff is not None
    assert handoff.url == "https://journals.aps.org/prl/abstract/10.1103/lk32-njx7"


def test_cloudflare_server_challenge_is_detected(tmp_path: Path):
    rec = make_record(url="https://publisher.example/paper")
    with requests_mock.Mocker() as m:
        m.get(
            "https://publisher.example/paper",
            status_code=503,
            headers={"Server": "cloudflare"},
            text="<html><title>Just a moment...</title></html>",
        )
        result, _, handoff = fetch_pdf_with_handoff(
            rec,
            cache_dir=tmp_path,
            unpaywall_email=None,
            scihub_mirror=None,
        )
    assert result is None
    assert handoff is not None
    assert handoff.url == "https://publisher.example/paper"


def test_all_sources_fail_returns_none(tmp_path: Path):
    rec = make_record(url=None, doi="")
    result, log = fetch_pdf(
        rec,
        cache_dir=tmp_path,
        unpaywall_email=None,
        scihub_mirror=None,
    )
    assert result is None
    # arxiv + publisher both attempted (no unpaywall email, no scihub).
    assert any(line.startswith("arxiv: failed") for line in log)
    assert any(line.startswith("publisher: failed") for line in log)


def test_cache_hit_skips_network(tmp_path: Path):
    rec = make_record(doi="10.1038/cached")
    # Pre-populate cache.
    cached = tmp_path / "10.1038_cached" / "paper.pdf"
    cached.parent.mkdir(parents=True)
    cached.write_bytes(PDF_BYTES)

    # No mocker → if the fetcher tried network, requests would explode.
    result, log = fetch_pdf(
        rec,
        cache_dir=tmp_path,
        unpaywall_email=None,
        scihub_mirror=None,
    )
    assert result is not None
    assert result.source == "cache"
    assert result.pdf_path == cached


def test_use_local_pdf_copies_into_cache(tmp_path: Path):
    rec = make_record()
    src = tmp_path / "src.pdf"
    src.write_bytes(PDF_BYTES)
    cache = tmp_path / "cache"
    result = use_local_pdf(src, rec, cache)
    assert result.source == "manual"
    assert result.pdf_path.exists()
    assert result.pdf_path.read_bytes() == PDF_BYTES


def test_http_get_dispatches_to_curl_cffi_when_enabled(monkeypatch):
    """When USE_CURL_CFFI is on and curl_cffi is importable, _http_get must
    route through it with a Chrome impersonation. This is the bypass path."""
    if not pdf_fetcher.HAS_CURL_CFFI:
        pytest.skip("curl_cffi not installed; install with [cloudflare] extra")

    captured: dict = {}

    class _StubModule:
        @staticmethod
        def get(url, **kwargs):
            captured["url"] = url
            captured["kwargs"] = kwargs
            return "stub-response"

    monkeypatch.setattr(pdf_fetcher, "USE_CURL_CFFI", True)
    monkeypatch.setattr(pdf_fetcher, "_cf_requests", _StubModule)

    resp = _http_get("https://example.com/x", headers={"X": "1"}, timeout=5.0, stream=True)
    assert resp == "stub-response"
    assert captured["url"] == "https://example.com/x"
    assert captured["kwargs"]["impersonate"] == pdf_fetcher.IMPERSONATE
    assert captured["kwargs"]["stream"] is True
    assert captured["kwargs"]["headers"] == {"X": "1"}


def test_http_get_falls_back_to_requests_when_curl_cffi_disabled(tmp_path: Path):
    """The conftest fixture flips USE_CURL_CFFI off; _http_get must then go
    through plain requests so requests_mock can intercept."""
    with requests_mock.Mocker() as m:
        m.get("https://example.com/probe", text="ok")
        resp = _http_get("https://example.com/probe", timeout=5.0)
    assert resp.status_code == 200
    assert resp.text == "ok"


def test_registered_domain_extracts_etld_plus_one():
    assert _registered_domain("https://journals.aps.org/prl/abstract/10.1103/x") == "aps.org"
    assert _registered_domain("https://www.nature.com/articles/x") == "nature.com"
    assert _registered_domain("https://example.com/x") == "example.com"
    # Single-label hosts (rare) fall through unchanged.
    assert _registered_domain("https://localhost/x") == "localhost"


def test_load_browser_cookies_raises_when_helper_missing(monkeypatch):
    monkeypatch.setattr(pdf_fetcher, "_bc3", None)
    with pytest.raises(CookieLoadError, match="not installed"):
        _load_browser_cookies("chrome", "https://journals.aps.org/prl/abstract/x")


def test_load_browser_cookies_raises_for_unsupported_browser(monkeypatch):
    class _StubBC3:
        pass  # no .chrome/.safari/etc. attributes

    monkeypatch.setattr(pdf_fetcher, "_bc3", _StubBC3())
    with pytest.raises(CookieLoadError, match="unsupported browser"):
        _load_browser_cookies("netscape", "https://example.com/x")


def test_load_browser_cookies_returns_dict_from_jar(monkeypatch):
    class _Cookie:
        def __init__(self, name, value):
            self.name = name
            self.value = value

    captured: dict = {}

    class _StubBC3:
        @staticmethod
        def chrome(domain_name):
            captured["domain_name"] = domain_name
            return [_Cookie("cf_clearance", "abc123"), _Cookie("session", "xyz")]

    monkeypatch.setattr(pdf_fetcher, "_bc3", _StubBC3())
    cookies = _load_browser_cookies("chrome", "https://journals.aps.org/prl/abstract/x")
    assert cookies == {"cf_clearance": "abc123", "session": "xyz"}
    # Lookup should target eTLD+1, not the full hostname, so cookies set on
    # .aps.org (the common case) get included.
    assert captured["domain_name"] == "aps.org"


def test_publisher_with_browser_cookies_sends_cookie_header(tmp_path: Path, monkeypatch):
    """End-to-end: when --browser-cookies is set, the publisher request must
    actually carry the loaded cookies in the Cookie header."""
    class _Cookie:
        def __init__(self, name, value):
            self.name = name
            self.value = value

    class _StubBC3:
        @staticmethod
        def chrome(domain_name):
            return [_Cookie("cf_clearance", "abc123")]

    monkeypatch.setattr(pdf_fetcher, "_bc3", _StubBC3())

    rec = make_record(url="https://journals.aps.org/prl/abstract/10.1103/lk32-njx7")
    landing_html = (
        '<html><head>'
        '<meta name="citation_pdf_url" content="https://journals.aps.org/prl/pdf/lk32-njx7">'
        '</head></html>'
    )
    with requests_mock.Mocker() as m:
        m.get("https://journals.aps.org/prl/abstract/10.1103/lk32-njx7", text=landing_html)
        m.get("https://journals.aps.org/prl/pdf/lk32-njx7", content=PDF_BYTES)
        result, log, handoff = fetch_pdf_with_handoff(
            rec,
            cache_dir=tmp_path,
            unpaywall_email=None,
            scihub_mirror=None,
            browser_cookies="chrome",
        )
        # Both the landing page and the PDF fetch should have carried the cookie.
        sent_cookies = [req.headers.get("Cookie", "") for req in m.request_history]

    assert result is not None and result.source == "publisher"
    assert handoff is None
    assert all("cf_clearance=abc123" in c for c in sent_cookies)


def test_use_local_pdf_rejects_non_pdf(tmp_path: Path):
    rec = make_record()
    src = tmp_path / "src.pdf"
    src.write_bytes(b"not a pdf")
    with pytest.raises(ValueError):
        use_local_pdf(src, rec, tmp_path / "cache")
