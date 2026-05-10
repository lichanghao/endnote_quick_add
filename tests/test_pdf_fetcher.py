from __future__ import annotations

from pathlib import Path

import pytest
import requests_mock

from endnote_quick_add.pdf_fetcher import (
    _extract_arxiv_id,
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


def test_use_local_pdf_rejects_non_pdf(tmp_path: Path):
    rec = make_record()
    src = tmp_path / "src.pdf"
    src.write_bytes(b"not a pdf")
    with pytest.raises(ValueError):
        use_local_pdf(src, rec, tmp_path / "cache")
