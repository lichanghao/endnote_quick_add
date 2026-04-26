from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .config import CONFIG_PATH, load_config
from .endnote import EndNoteNotFound, import_to_endnote
from .pdf_fetcher import fetch_pdf, use_local_pdf
from .resolver import (
    CrossRefRecord,
    fetch_by_doi,
    is_doi,
    search_by_title,
)
from .ris_writer import write_ris


def _pick(records: list[CrossRefRecord]) -> CrossRefRecord:
    if not records:
        print("No matches found on CrossRef.", file=sys.stderr)
        sys.exit(1)
    print(f"Found {len(records)} matches:\n")
    for i, r in enumerate(records, 1):
        print(f"  [{i}] {r.summary()}")
        if r.doi:
            print(f"       doi: {r.doi}")
    print()
    while True:
        choice = input(f"Pick a number (1-{len(records)}, blank=1, q=quit): ").strip()
        if choice.lower() in ("q", "quit", "exit"):
            sys.exit(0)
        if not choice:
            return records[0]
        if choice.isdigit() and 1 <= int(choice) <= len(records):
            return records[int(choice) - 1]
        print("invalid selection")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="eqa",
        description="EndNote Quick-Add: resolve a paper, fetch its PDF, hand both to EndNote.",
    )
    p.add_argument("query", nargs="+", help="DOI or paper title (free text).")
    p.add_argument("--pdf", help="URL or local path to a PDF; skips the source chain.", default=None)
    p.add_argument("--no-pdf", action="store_true", help="Don't attach a PDF; import citation only.")
    p.add_argument("--app", help="EndNote application name (overrides config).")
    p.add_argument("--dry-run", action="store_true", help="Show what would happen but don't import to EndNote.")
    p.add_argument("--config", help="Path to config TOML.", default=None)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config_path = Path(args.config).expanduser() if args.config else CONFIG_PATH
    cfg = load_config(config_path)
    app_name = args.app or cfg.endnote_app

    query = " ".join(args.query).strip()

    # 1. Resolve metadata.
    if is_doi(query):
        try:
            record = fetch_by_doi(query, email=cfg.email if cfg.has_unpaywall else None)
        except LookupError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        print(f"Resolved DOI → {record.summary()}")
    else:
        candidates = search_by_title(
            query,
            rows=5,
            email=cfg.email if cfg.has_unpaywall else None,
        )
        record = _pick(candidates)
        print(f"Selected → {record.summary()}")

    # 2. Fetch PDF.
    pdf_path = None
    fetch_log: list[str] = []
    if args.no_pdf:
        print("(skipping PDF — --no-pdf)")
    else:
        if args.pdf and not args.pdf.startswith(("http://", "https://")):
            local = Path(args.pdf).expanduser()
            if not local.exists():
                print(f"error: --pdf path {local} does not exist", file=sys.stderr)
                return 1
            result = use_local_pdf(local, record, cfg.cache_dir)
            pdf_path = result.pdf_path
            print(f"PDF: {pdf_path} (source: manual local file)")
        else:
            result, fetch_log = fetch_pdf(
                record,
                cache_dir=cfg.cache_dir,
                unpaywall_email=cfg.email if cfg.has_unpaywall else None,
                scihub_mirror=cfg.scihub_mirror if cfg.has_scihub else None,
                override_url=args.pdf,
            )
            for line in fetch_log:
                print(f"  {line}")
            if result is None:
                print("\nNo PDF could be downloaded; importing citation only.\n")
            else:
                pdf_path = result.pdf_path
                print(f"PDF: {pdf_path} (source: {result.source})")

    # 3. Write RIS.
    ris_dir = cfg.cache_dir / (record.doi.replace("/", "_") if record.doi else "manual")
    ris_path = write_ris(record, ris_dir, pdf_path=pdf_path)
    print(f"RIS: {ris_path}")

    # 4. Import to EndNote.
    if args.dry_run:
        print("(dry-run — not opening EndNote)")
        return 0

    try:
        import_to_endnote(ris_path, app_name=app_name)
    except EndNoteNotFound as e:
        print(f"error: {e}", file=sys.stderr)
        print("Hint: pass --app 'EndNote 20' (or your version) or set endnote_app in config.", file=sys.stderr)
        return 2

    print(f"\nHanded off to {app_name}. Confirm the import dialog.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
