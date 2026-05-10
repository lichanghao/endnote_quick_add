from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from .resolver import CrossRefRecord

try:
    from curl_cffi import requests as _cf_requests  # type: ignore
    HAS_CURL_CFFI = True
except ImportError:
    _cf_requests = None
    HAS_CURL_CFFI = False

try:
    import browser_cookie3 as _bc3  # type: ignore
    HAS_BROWSER_COOKIE3 = True
except ImportError:
    _bc3 = None
    HAS_BROWSER_COOKIE3 = False

# When True (and curl_cffi is installed), publisher/Sci-Hub requests go through
# curl_cffi with a real Chrome TLS/JA3 fingerprint. That sidesteps the most
# common Cloudflare block, which keys off TLS-fingerprint mismatch with the
# claimed User-Agent. Tests flip this off so requests_mock keeps working.
USE_CURL_CFFI = HAS_CURL_CFFI
IMPERSONATE = "chrome124"


class CookieLoadError(RuntimeError):
    pass


def _registered_domain(url: str) -> str:
    """Best-effort eTLD+1 for cookie lookup. Good enough for journal hosts
    like journals.aps.org (-> aps.org) and www.nature.com (-> nature.com)."""
    host = urlparse(url).hostname or ""
    parts = host.split(".")
    if len(parts) <= 2:
        return host
    return ".".join(parts[-2:])


def _load_browser_cookies(browser: str, url: str) -> dict[str, str]:
    """Pull cookies for `url`'s registered domain from the user's real browser.

    Triggers a macOS Keychain prompt the first time it reads Chrome cookies —
    click "Always Allow" to suppress on subsequent runs. Raises CookieLoadError
    on any failure so the orchestrator can surface a useful attempt-log entry."""
    if _bc3 is None:
        raise CookieLoadError(
            "browser_cookie3 is not installed; "
            "install with: pip install 'endnote-quick-add[cloudflare]'"
        )
    fn = getattr(_bc3, browser.lower(), None)
    if fn is None:
        raise CookieLoadError(f"unsupported browser: {browser!r}")
    domain = _registered_domain(url)
    try:
        jar = fn(domain_name=domain)
    except Exception as e:
        raise CookieLoadError(f"could not read cookies from {browser} for {domain}: {e}") from e
    return {c.name: c.value for c in jar}

ARXIV_DOI_RE = re.compile(r"10\.48550/arxiv\.([\w.\-/]+)", re.IGNORECASE)
ARXIV_URL_RE = re.compile(r"arxiv\.org/(?:abs|pdf)/([\w.\-/]+?)(?:v\d+)?(?:\.pdf)?(?:[/?#]|$)", re.IGNORECASE)
PDF_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0 Safari/537.36"
    ),
    "Accept": "application/pdf,*/*;q=0.8",
}


def _http_get(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    params: dict[str, str] | None = None,
    cookies: dict[str, str] | None = None,
    timeout: float = 20.0,
    stream: bool = False,
    allow_redirects: bool = True,
):
    """Dispatch to curl_cffi (with browser TLS impersonation) or plain requests."""
    if USE_CURL_CFFI and _cf_requests is not None:
        return _cf_requests.get(
            url,
            headers=headers,
            params=params,
            cookies=cookies,
            timeout=timeout,
            stream=stream,
            allow_redirects=allow_redirects,
            impersonate=IMPERSONATE,
        )
    return requests.get(
        url,
        headers=headers,
        params=params,
        cookies=cookies,
        timeout=timeout,
        stream=stream,
        allow_redirects=allow_redirects,
    )


@dataclass
class BrowserHandoff:
    url: str
    reason: str


class CloudflareBlocked(PermissionError):
    def __init__(self, message: str, *, url: str):
        super().__init__(message)
        self.url = url


def _is_cloudflare_challenge(resp) -> bool:
    if resp.headers.get("cf-mitigated", "").lower() == "challenge":
        return True
    if resp.status_code not in {403, 429, 503}:
        return False
    if "cloudflare" not in resp.headers.get("Server", "").lower():
        return False
    body = resp.text[:4096].lower()
    return any(marker in body for marker in ("cloudflare", "just a moment", "challenge"))


def _raise_for_status(resp) -> None:
    try:
        resp.raise_for_status()
    except Exception as e:
        # Both requests.HTTPError and curl_cffi's HTTPError land here.
        if _is_cloudflare_challenge(resp):
            raise CloudflareBlocked(
                "server blocked the automated request with a Cloudflare challenge; "
                "opening the article in a normal browser is required for manual access",
                url=resp.url,
            ) from e
        raise


@dataclass
class FetchResult:
    pdf_path: Path
    source: str  # "arxiv" | "unpaywall" | "publisher" | "scihub" | "manual"


def _doi_slug(doi: str) -> str:
    return re.sub(r"[^\w.-]+", "_", doi)


def _cache_paths(doi: str, cache_dir: Path) -> tuple[Path, Path]:
    slug_dir = cache_dir / _doi_slug(doi)
    slug_dir.mkdir(parents=True, exist_ok=True)
    return slug_dir, slug_dir / "paper.pdf"


def _browser_handoff_url(record: CrossRefRecord, blocked_url: str | None = None) -> str:
    if record.url:
        return record.url
    if record.doi:
        return f"https://doi.org/{record.doi}"
    return blocked_url or ""


def _looks_like_pdf(resp) -> bool:
    ct = resp.headers.get("Content-Type", "").lower()
    if "application/pdf" in ct:
        return True
    # Some servers send octet-stream for PDFs.
    if "application/octet-stream" in ct and resp.content[:4] == b"%PDF":
        return True
    return False


def _download(
    url: str, dest: Path, *, timeout: float = 30.0, cookies: dict[str, str] | None = None
) -> None:
    with _http_get(
        url, headers=PDF_BROWSER_HEADERS, stream=True, timeout=timeout, cookies=cookies
    ) as r:
        _raise_for_status(r)
        # Quick sanity check on first chunk.
        first = next(r.iter_content(8192), b"")
        if not first.startswith(b"%PDF"):
            raise ValueError(f"response from {url} is not a PDF (starts with {first[:8]!r})")
        with dest.open("wb") as f:
            f.write(first)
            for chunk in r.iter_content(64 * 1024):
                if chunk:
                    f.write(chunk)


def _extract_arxiv_id(record: CrossRefRecord) -> str | None:
    if record.doi:
        m = ARXIV_DOI_RE.search(record.doi)
        if m:
            return m.group(1)
    # Search relation field for arXiv preprint link.
    relation = record.raw.get("relation") or {}
    for rels in relation.values():
        if not isinstance(rels, list):
            continue
        for rel in rels:
            ident = (rel or {}).get("id", "")
            m = ARXIV_DOI_RE.search(ident) or ARXIV_URL_RE.search(ident)
            if m:
                return m.group(1)
    if record.url:
        m = ARXIV_URL_RE.search(record.url)
        if m:
            return m.group(1)
    return None


# --- Source functions ----------------------------------------------------

def try_arxiv(record: CrossRefRecord, dest: Path) -> bool:
    arxiv_id = _extract_arxiv_id(record)
    if not arxiv_id:
        raise LookupError("no arXiv id detected in record")
    url = f"https://arxiv.org/pdf/{arxiv_id}.pdf"
    _download(url, dest)
    return True


def try_unpaywall(record: CrossRefRecord, dest: Path, *, email: str) -> bool:
    if not record.doi:
        raise LookupError("record has no DOI")
    api = f"https://api.unpaywall.org/v2/{record.doi}"
    r = requests.get(api, params={"email": email}, timeout=15)
    if r.status_code == 404:
        raise LookupError("Unpaywall: DOI not indexed")
    _raise_for_status(r)
    data = r.json()
    loc = data.get("best_oa_location") or {}
    pdf_url = loc.get("url_for_pdf") or loc.get("url")
    if not pdf_url:
        raise LookupError("Unpaywall: no open-access PDF location")
    _download(pdf_url, dest)
    return True


def _meta_pdf_url(html: str, base_url: str) -> str | None:
    soup = BeautifulSoup(html, "html.parser")
    for name in ("citation_pdf_url", "prism.url", "eprints.document_url"):
        tag = soup.find("meta", attrs={"name": name})
        if tag and tag.get("content"):
            return urljoin(base_url, tag["content"])
    # Fallback: anchor whose href ends with .pdf.
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if href.lower().endswith(".pdf") or "/pdf/" in href.lower():
            return urljoin(base_url, href)
    return None


def try_publisher(
    record: CrossRefRecord, dest: Path, *, browser_cookies: str | None = None
) -> bool:
    if not record.url:
        raise LookupError("record has no publisher URL")
    cookies = _load_browser_cookies(browser_cookies, record.url) if browser_cookies else None
    r = _http_get(record.url, headers=PDF_BROWSER_HEADERS, timeout=20, cookies=cookies)
    _raise_for_status(r)
    if _looks_like_pdf(r):
        dest.write_bytes(r.content)
        if not dest.read_bytes().startswith(b"%PDF"):
            dest.unlink(missing_ok=True)
            raise ValueError("publisher returned a non-PDF body despite content-type")
        return True
    pdf_url = _meta_pdf_url(r.text, r.url)
    if not pdf_url:
        raise LookupError("publisher page has no citation_pdf_url meta tag or PDF link")
    # Reuse the same cookies for the PDF fetch — same registered domain in
    # nearly all cases (publisher landing → publisher CDN), and even when not,
    # extra cookies are harmless.
    _download(pdf_url, dest, cookies=cookies)
    return True


def try_scihub(
    record: CrossRefRecord,
    dest: Path,
    *,
    mirror: str,
    browser_cookies: str | None = None,
) -> bool:
    if not record.doi:
        raise LookupError("record has no DOI")
    base = mirror.rstrip("/")
    page = f"{base}/{record.doi}"
    cookies = _load_browser_cookies(browser_cookies, page) if browser_cookies else None
    r = _http_get(page, headers=PDF_BROWSER_HEADERS, timeout=20, cookies=cookies)
    _raise_for_status(r)
    soup = BeautifulSoup(r.text, "html.parser")
    src = None
    for tag_name in ("embed", "iframe"):
        tag = soup.find(tag_name)
        if tag and tag.get("src"):
            src = tag["src"]
            break
    if not src:
        raise LookupError("Sci-Hub mirror returned no embed/iframe with a PDF source")
    if src.startswith("//"):
        scheme = urlparse(r.url).scheme or "https"
        src = f"{scheme}:{src}"
    src = urljoin(r.url, src)
    # Strip fragment (Sci-Hub appends #view=FitH etc.).
    src = src.split("#", 1)[0]
    _download(src, dest, cookies=cookies)
    return True


# --- Orchestrator --------------------------------------------------------

def use_local_pdf(local: Path, record: CrossRefRecord, cache_dir: Path) -> FetchResult:
    _, dest = _cache_paths(record.doi or "manual", cache_dir)
    if local.resolve() != dest.resolve():
        shutil.copyfile(local, dest)
    if not dest.read_bytes().startswith(b"%PDF"):
        raise ValueError(f"{local} does not look like a PDF (no %PDF header)")
    return FetchResult(pdf_path=dest, source="manual")


def fetch_pdf(
    record: CrossRefRecord,
    *,
    cache_dir: Path,
    unpaywall_email: str | None,
    scihub_mirror: str | None,
    override_url: str | None = None,
    browser_cookies: str | None = None,
) -> tuple[FetchResult | None, list[str]]:
    result, log, _ = fetch_pdf_with_handoff(
        record,
        cache_dir=cache_dir,
        unpaywall_email=unpaywall_email,
        scihub_mirror=scihub_mirror,
        override_url=override_url,
        browser_cookies=browser_cookies,
    )
    return result, log


def fetch_pdf_with_handoff(
    record: CrossRefRecord,
    *,
    cache_dir: Path,
    unpaywall_email: str | None,
    scihub_mirror: str | None,
    override_url: str | None = None,
    browser_cookies: str | None = None,
) -> tuple[FetchResult | None, list[str], BrowserHandoff | None]:
    """Try each PDF source in order.

    Returns (result_or_None, attempt_log, browser_handoff_or_None).
    """
    _, dest = _cache_paths(record.doi or "unknown", cache_dir)

    if dest.exists() and dest.stat().st_size > 0 and dest.read_bytes()[:4] == b"%PDF":
        return FetchResult(pdf_path=dest, source="cache"), ["cache: hit"], None

    log: list[str] = []
    handoff: BrowserHandoff | None = None

    if override_url:
        # No cookies on a manual override URL — the user already picked it.
        try:
            _download(override_url, dest)
            return FetchResult(pdf_path=dest, source="manual"), [f"manual: {override_url}"], None
        except CloudflareBlocked as e:
            log.append(f"manual: failed ({e})")
            return None, log, BrowserHandoff(url=e.url or override_url, reason=str(e))
        except Exception as e:
            log.append(f"manual: failed ({e})")
            return None, log, None

    sources: list[tuple[str, Callable[[], bool]]] = [
        ("arxiv", lambda: try_arxiv(record, dest)),
    ]
    if unpaywall_email:
        sources.append(("unpaywall", lambda: try_unpaywall(record, dest, email=unpaywall_email)))
    sources.append(("publisher", lambda: try_publisher(record, dest, browser_cookies=browser_cookies)))
    if scihub_mirror:
        sources.append(("scihub", lambda: try_scihub(record, dest, mirror=scihub_mirror, browser_cookies=browser_cookies)))

    for name, fn in sources:
        try:
            fn()
            log.append(f"{name}: ok")
            return FetchResult(pdf_path=dest, source=name), log, handoff
        except CloudflareBlocked as e:
            log.append(f"{name}: failed ({e})")
            if handoff is None:
                handoff_url = _browser_handoff_url(record, e.url)
                if handoff_url:
                    handoff = BrowserHandoff(url=handoff_url, reason=str(e))
            # Clean up partial download so the next source starts clean.
            if dest.exists() and (dest.stat().st_size == 0 or not dest.read_bytes()[:4] == b"%PDF"):
                dest.unlink(missing_ok=True)
        except Exception as e:
            log.append(f"{name}: failed ({e})")
            # Clean up partial download so the next source starts clean.
            if dest.exists() and (dest.stat().st_size == 0 or not dest.read_bytes()[:4] == b"%PDF"):
                dest.unlink(missing_ok=True)

    return None, log, handoff
