# endnote-quick-add (`eqa`)

A fast Mac CLI for adding a paper to your EndNote library. Type a title or DOI;
it resolves the citation, downloads the PDF from the first source that has it,
writes a RIS file with the PDF attached, and hands it to EndNote.

## Install

Requires Python 3.10+ and EndNote on macOS.

```bash
pipx install -e /path/to/endnote_quick_add_tool
```

(or `pip install -e .` inside a venv)

The first run creates a config template at
`~/.config/endnote_quick_add/config.toml`. Edit it — at minimum set `email`
(Unpaywall requires it) and `endnote_app` to your installed version, e.g.
`"EndNote 21"`.

```toml
email = "you@example.com"
endnote_app = "EndNote 21"
scihub_mirror = ""                    # optional last-resort mirror
cache_dir = "~/.cache/endnote_quick_add"
```

## Usage

```bash
# By DOI:
eqa 10.1038/s41586-020-2649-2

# By title (interactive picker shows top 5):
eqa "attention is all you need"

# Override the PDF source with a URL or local file:
eqa --pdf ~/Downloads/paper.pdf 10.1038/s41586-020-2649-2
eqa --pdf https://example.com/paper.pdf 10.1038/s41586-020-2649-2

# Citation only, skip PDF:
eqa --no-pdf 10.1038/s41586-020-2649-2

# See what would happen, without opening EndNote:
eqa --dry-run "attention is all you need"

# Override EndNote app name (default comes from config):
eqa --app "EndNote 20" 10.1038/s41586-020-2649-2
```

## How it works

1. **Resolve** the input via the CrossRef API. DOI → direct lookup; title →
   top-5 picker.
2. **Fetch the PDF** by trying sources in order, stopping at the first hit:
   - **arXiv** if the record points to a preprint (e.g. `10.48550/arXiv.*`).
   - **Unpaywall** for open-access copies.
   - **Publisher URL** directly (works on always-on VPN / on-campus). The tool
     looks for `<meta name="citation_pdf_url">` on the landing page.
   - **Sci-Hub** mirror (optional, configured in `scihub_mirror`).
3. **Write a RIS file** mapping CrossRef metadata to RIS tags, with `L1  -
   file://...` so EndNote attaches the PDF.
4. **Hand off to EndNote** via `open -a "EndNote 21" citation.ris`. EndNote's
   import dialog appears; confirm to add to the current library.

PDFs and RIS files are cached under `cache_dir` keyed by DOI, so re-running
the same query is free.

### Note on arXiv-only DOIs

CrossRef coverage of `10.48550/arXiv.*` DOIs is patchy, especially for older
preprints. If a DOI lookup returns "no record", search by title instead — the
top hit is usually the published version (which CrossRef *does* have) or an
indexed preprint mirror. From there, the arXiv source still kicks in for the
PDF download.

## Development

```bash
pip install -e ".[dev]"
pytest
```
