from __future__ import annotations

from pathlib import Path

from .resolver import CrossRefRecord

CROSSREF_TO_RIS_TYPE = {
    "journal-article": "JOUR",
    "proceedings-article": "CPAPER",
    "book-chapter": "CHAP",
    "book": "BOOK",
    "monograph": "BOOK",
    "report": "RPRT",
    "dataset": "DATA",
    "posted-content": "JOUR",  # preprint — EndNote handles JOUR fine
    "dissertation": "THES",
}


def _ris_line(tag: str, value: str | None) -> str | None:
    if value is None or value == "":
        return None
    return f"{tag}  - {value}"


def _split_authors(authors: list[str]) -> list[str]:
    # RIS expects "Family, Given" per AU line.
    out = []
    for full in authors:
        parts = full.rsplit(" ", 1)
        if len(parts) == 2 and parts[1]:
            given, family = parts[0], parts[1]
            # Heuristic only for "Given Family"; CrossRef gives us "Given Family".
            out.append(f"{family}, {given}")
        else:
            out.append(full)
    return out


def _split_pages(page: str | None) -> tuple[str | None, str | None]:
    if not page:
        return None, None
    if "-" in page:
        sp, ep = page.split("-", 1)
        return sp.strip(), ep.strip()
    return page.strip(), None


def build_ris(record: CrossRefRecord, pdf_path: Path | None = None) -> str:
    ris_type = CROSSREF_TO_RIS_TYPE.get(record.type or "", "JOUR")
    sp, ep = _split_pages(record.page)

    lines: list[str | None] = [f"TY  - {ris_type}"]
    for au in _split_authors(record.authors):
        lines.append(_ris_line("AU", au))
    lines.extend([
        _ris_line("TI", record.title),
        _ris_line("T2", record.container_title),
        _ris_line("JF", record.container_title),
        _ris_line("VL", record.volume),
        _ris_line("IS", record.issue),
        _ris_line("SP", sp),
        _ris_line("EP", ep),
        _ris_line("PY", record.year),
        _ris_line("DO", record.doi),
        _ris_line("UR", record.url),
        _ris_line("PB", record.publisher),
    ])
    if pdf_path is not None:
        lines.append(_ris_line("L1", f"file://{pdf_path.resolve()}"))
    lines.append("ER  - ")

    return "\n".join(line for line in lines if line) + "\n"


def write_ris(record: CrossRefRecord, out_dir: Path, pdf_path: Path | None = None) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "citation.ris"
    out_path.write_text(build_ris(record, pdf_path), encoding="utf-8")
    return out_path
