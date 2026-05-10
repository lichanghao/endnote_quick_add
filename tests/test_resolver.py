from __future__ import annotations

import pytest
import requests_mock

from endnote_quick_add.resolver import (
    fetch_by_doi,
    is_doi,
    normalize_doi,
    search_by_title,
)


def test_is_doi_recognizes_plain_and_prefixed_forms():
    assert is_doi("10.1038/s41586-020-2649-2")
    assert is_doi("doi:10.1038/s41586-020-2649-2")
    assert is_doi("https://doi.org/10.1038/s41586-020-2649-2")
    assert is_doi("https://journals.aps.org/prl/abstract/10.1103/lk32-njx7")
    assert not is_doi("attention is all you need")
    assert not is_doi("10.bad")


def test_normalize_doi_strips_prefixes():
    assert normalize_doi("doi:10.1/abc") == "10.1/abc"
    assert normalize_doi("https://doi.org/10.1/abc") == "10.1/abc"
    assert normalize_doi("https://journals.aps.org/prl/abstract/10.1103/lk32-njx7") == "10.1103/lk32-njx7"
    assert normalize_doi("10.1/abc") == "10.1/abc"


@pytest.fixture
def crossref_doi_payload():
    return {
        "status": "ok",
        "message": {
            "DOI": "10.1038/test",
            "title": ["A Test Paper"],
            "author": [
                {"given": "Ada", "family": "Lovelace"},
                {"given": "Alan", "family": "Turing"},
            ],
            "issued": {"date-parts": [[2024, 5]]},
            "container-title": ["Journal of Tests"],
            "volume": "12",
            "issue": "3",
            "page": "100-110",
            "publisher": "Nature",
            "type": "journal-article",
            "URL": "https://doi.org/10.1038/test",
        },
    }


def test_fetch_by_doi_returns_populated_record(crossref_doi_payload):
    with requests_mock.Mocker() as m:
        m.get(
            "https://api.crossref.org/works/10.1038/test",
            json=crossref_doi_payload,
        )
        rec = fetch_by_doi("10.1038/test", email="user@example.com")

    assert rec.doi == "10.1038/test"
    assert rec.title == "A Test Paper"
    assert rec.authors == ["Ada Lovelace", "Alan Turing"]
    assert rec.year == "2024"
    assert rec.container_title == "Journal of Tests"
    assert rec.volume == "12"
    assert rec.page == "100-110"
    assert rec.short_authors == "Ada Lovelace and Alan Turing"


def test_fetch_by_doi_404_raises():
    with requests_mock.Mocker() as m:
        m.get("https://api.crossref.org/works/10.1/missing", status_code=404)
        with pytest.raises(LookupError):
            fetch_by_doi("10.1/missing")


def test_search_by_title_returns_records():
    payload = {
        "message": {
            "items": [
                {
                    "DOI": "10.1/a",
                    "title": ["Paper A"],
                    "author": [{"given": "Foo", "family": "Bar"}],
                    "issued": {"date-parts": [[2020]]},
                    "container-title": ["Journal A"],
                    "type": "journal-article",
                    "URL": "https://example.com/a",
                },
                {
                    "DOI": "10.1/b",
                    "title": ["Paper B"],
                    "author": [],
                    "issued": {"date-parts": [[2019]]},
                    "container-title": ["Journal B"],
                    "type": "journal-article",
                    "URL": "https://example.com/b",
                },
            ]
        }
    }
    with requests_mock.Mocker() as m:
        m.get("https://api.crossref.org/works", json=payload)
        results = search_by_title("paper", rows=5)
    assert [r.doi for r in results] == ["10.1/a", "10.1/b"]
    assert results[1].short_authors == "Unknown"
