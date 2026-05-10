from __future__ import annotations

import pytest

from endnote_quick_add import pdf_fetcher


@pytest.fixture(autouse=True)
def _force_requests_transport(monkeypatch):
    """Tests use requests_mock to stub HTTP; force the requests transport so
    the curl_cffi path (which requests_mock cannot intercept) doesn't fire."""
    monkeypatch.setattr(pdf_fetcher, "USE_CURL_CFFI", False)
