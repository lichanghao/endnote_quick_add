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


def _looks_like_pdf(resp: requests.Response) -> bool:
    ct = resp.headers.get("Content-Type", "").lower()
    if "application/pdf" in ct:
        return True
    # Some servers send octet-stream for PDFs.
    if "application/octet-stream" in ct and resp.content[:4] == b"%PDF":
        return True
    return False


def _download(url: str, dest: Path, *, timeout: float = 30.0) -> None:
    with requests.get(url, headers=PDF_BROWSER_HEADERS, stream=True, timeout=timeout, allow_redirects=True) as r:
        r.raise_for_status()
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
    r.raise_for_status()
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


def try_publisher(record: CrossRefRecord, dest: Path) -> bool:
    if not record.url:
        raise LookupError("record has no publisher URL")
    r = requests.get(record.url, headers=PDF_BROWSER_HEADERS, timeout=20, allow_redirects=True)
    r.raise_for_status()
    if _looks_like_pdf(r):
        dest.write_bytes(r.content)
        if not dest.read_bytes().startswith(b"%PDF"):
            dest.unlink(missing_ok=True)
            raise ValueError("publisher returned a non-PDF body despite content-type")
        return True
    pdf_url = _meta_pdf_url(r.text, r.url)
    if not pdf_url:
        raise LookupError("publisher page has no citation_pdf_url meta tag or PDF link")
    _download(pdf_url, dest)
    return True


def try_scihub(record: CrossRefRecord, dest: Path, *, mirror: str) -> bool:
    if not record.doi:
        raise LookupError("record has no DOI")
    base = mirror.rstrip("/")
    page = f"{base}/{record.doi}"
    r = requests.get(page, headers=PDF_BROWSER_HEADERS, timeout=20, allow_redirects=True)
    r.raise_for_status()
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
    _download(src, dest)
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
) -> tuple[FetchResult | None, list[str]]:
    """Try each PDF source in order. Returns (result_or_None, attempt_log)."""
    _, dest = _cache_paths(record.doi or "unknown", cache_dir)

    if dest.exists() and dest.stat().st_size > 0 and dest.read_bytes()[:4] == b"%PDF":
        return FetchResult(pdf_path=dest, source="cache"), ["cache: hit"]

    log: list[str] = []

    if override_url:
        try:
            _download(override_url, dest)
            return FetchResult(pdf_path=dest, source="manual"), [f"manual: {override_url}"]
        except Exception as e:
            log.append(f"manual: failed ({e})")
            return None, log

    sources: list[tuple[str, Callable[[], bool]]] = [
        ("arxiv", lambda: try_arxiv(record, dest)),
    ]
    if unpaywall_email:
        sources.append(("unpaywall", lambda: try_unpaywall(record, dest, email=unpaywall_email)))
    sources.append(("publisher", lambda: try_publisher(record, dest)))
    if scihub_mirror:
        sources.append(("scihub", lambda: try_scihub(record, dest, mirror=scihub_mirror)))

    for name, fn in sources:
        try:
            fn()
            log.append(f"{name}: ok")
            return FetchResult(pdf_path=dest, source=name), log
        except Exception as e:
            log.append(f"{name}: failed ({e})")
            # Clean up partial download so the next source starts clean.
            if dest.exists() and (dest.stat().st_size == 0 or not dest.read_bytes()[:4] == b"%PDF"):
                dest.unlink(missing_ok=True)

    return None, log
