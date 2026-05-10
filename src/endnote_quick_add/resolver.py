from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import requests

CROSSREF_BASE = "https://api.crossref.org/works"
DOI_RE = re.compile(r"^10\.\d{1,9}/\S+$")
DOI_IN_TEXT_RE = re.compile(r"10\.\d{1,9}/[^\s?#]+", re.IGNORECASE)


@dataclass
class CrossRefRecord:
    doi: str
    title: str
    authors: list[str]
    year: str | None
    container_title: str | None
    volume: str | None
    issue: str | None
    page: str | None
    publisher: str | None
    type: str | None
    url: str | None
    raw: dict[str, Any] = field(repr=False, default_factory=dict)

    @property
    def short_authors(self) -> str:
        if not self.authors:
            return "Unknown"
        if len(self.authors) == 1:
            return self.authors[0]
        if len(self.authors) == 2:
            return f"{self.authors[0]} and {self.authors[1]}"
        return f"{self.authors[0]} et al."

    def summary(self) -> str:
        bits = [self.short_authors]
        if self.year:
            bits.append(f"({self.year})")
        bits.append(f'"{self.title}"')
        if self.container_title:
            bits.append(f"— {self.container_title}")
        return " ".join(bits)


def is_doi(s: str) -> bool:
    return bool(DOI_RE.match(normalize_doi(s)))


def normalize_doi(s: str) -> str:
    s = s.strip()
    if s.lower().startswith("doi:"):
        s = s[4:]
    match = DOI_IN_TEXT_RE.search(s)
    if not match:
        return s
    return match.group(0).rstrip(".,;)")


def _record_from_message(msg: dict[str, Any]) -> CrossRefRecord:
    title_list = msg.get("title") or []
    title = title_list[0] if title_list else "(untitled)"

    authors: list[str] = []
    for a in msg.get("author", []) or []:
        given = a.get("given", "").strip()
        family = a.get("family", "").strip()
        name = " ".join(p for p in [given, family] if p) or a.get("name", "")
        if name:
            authors.append(name)

    year = None
    for key in ("published-print", "published-online", "issued", "created"):
        parts = msg.get(key, {}).get("date-parts")
        if parts and parts[0]:
            year = str(parts[0][0])
            break

    container_list = msg.get("container-title") or []
    container = container_list[0] if container_list else None

    return CrossRefRecord(
        doi=msg.get("DOI", ""),
        title=title,
        authors=authors,
        year=year,
        container_title=container,
        volume=msg.get("volume"),
        issue=msg.get("issue"),
        page=msg.get("page"),
        publisher=msg.get("publisher"),
        type=msg.get("type"),
        url=msg.get("URL"),
        raw=msg,
    )


def _user_agent(email: str | None) -> str:
    contact = f"mailto:{email}" if email else "https://github.com/"
    return f"endnote_quick_add/0.1.0 ({contact})"


def fetch_by_doi(doi: str, *, email: str | None = None, timeout: float = 15.0) -> CrossRefRecord:
    doi = normalize_doi(doi)
    headers = {"User-Agent": _user_agent(email)}
    r = requests.get(f"{CROSSREF_BASE}/{doi}", headers=headers, timeout=timeout)
    if r.status_code == 404:
        raise LookupError(f"CrossRef has no record for DOI {doi}")
    r.raise_for_status()
    return _record_from_message(r.json()["message"])


def search_by_title(
    title: str,
    *,
    rows: int = 5,
    email: str | None = None,
    timeout: float = 15.0,
) -> list[CrossRefRecord]:
    headers = {"User-Agent": _user_agent(email)}
    params = {"query.bibliographic": title, "rows": str(rows)}
    r = requests.get(CROSSREF_BASE, headers=headers, params=params, timeout=timeout)
    r.raise_for_status()
    items = r.json().get("message", {}).get("items", [])
    return [_record_from_message(it) for it in items]
