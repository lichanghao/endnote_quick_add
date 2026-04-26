from __future__ import annotations

from pathlib import Path

from endnote_quick_add.resolver import CrossRefRecord
from endnote_quick_add.ris_writer import build_ris, write_ris


def make_record() -> CrossRefRecord:
    return CrossRefRecord(
        doi="10.1038/test",
        title="A Test Paper",
        authors=["Ada Lovelace", "Alan Turing"],
        year="2024",
        container_title="Journal of Tests",
        volume="12",
        issue="3",
        page="100-110",
        publisher="Nature",
        type="journal-article",
        url="https://doi.org/10.1038/test",
        raw={},
    )


def test_build_ris_maps_core_fields():
    ris = build_ris(make_record())
    assert ris.startswith("TY  - JOUR\n")
    assert "AU  - Lovelace, Ada" in ris
    assert "AU  - Turing, Alan" in ris
    assert "TI  - A Test Paper" in ris
    assert "T2  - Journal of Tests" in ris
    assert "VL  - 12" in ris
    assert "IS  - 3" in ris
    assert "SP  - 100" in ris
    assert "EP  - 110" in ris
    assert "PY  - 2024" in ris
    assert "DO  - 10.1038/test" in ris
    assert ris.rstrip().endswith("ER  -")


def test_build_ris_adds_l1_for_pdf(tmp_path: Path):
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-fake")
    ris = build_ris(make_record(), pdf_path=pdf)
    assert f"L1  - file://{pdf.resolve()}" in ris


def test_build_ris_skips_l1_when_no_pdf():
    ris = build_ris(make_record())
    assert "L1  -" not in ris


def test_write_ris_creates_file(tmp_path: Path):
    out = write_ris(make_record(), tmp_path)
    assert out.exists()
    assert out.read_text().startswith("TY  - JOUR")


def test_book_chapter_maps_to_chap():
    rec = make_record()
    rec.type = "book-chapter"
    assert build_ris(rec).startswith("TY  - CHAP\n")


def test_unknown_type_falls_back_to_jour():
    rec = make_record()
    rec.type = "weird-future-type"
    assert build_ris(rec).startswith("TY  - JOUR\n")
