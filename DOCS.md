# `endnote-quick-add` — Documentation

Internals and reference for the `eqa` CLI. For install + quickstart, see
[`README.md`](README.md).

---

## Table of contents

1. [Overview](#overview)
2. [Architecture](#architecture)
3. [Module reference](#module-reference)
4. [Configuration](#configuration)
5. [PDF source chain](#pdf-source-chain)
6. [CrossRef → RIS field mapping](#crossref--ris-field-mapping)
7. [Caching](#caching)
8. [CLI reference](#cli-reference)
9. [Testing](#testing)
10. [Extending](#extending)
11. [Limitations and known issues](#limitations-and-known-issues)

---

## Overview

`eqa` collapses the manual workflow

> *search the paper → find a PDF → download it → find/export a citation → drag both into EndNote*

into a single command:

```bash
eqa "attention is all you need"
```

The tool resolves the paper via CrossRef, fetches the PDF from whichever
source has it (arXiv / Unpaywall / publisher / Sci-Hub), generates an RIS
citation file with the PDF attached via the `L1` tag, and hands the file to
EndNote with `open -a`. EndNote's import dialog adds the entry + linked PDF
to the currently-open library.

**Stack.** Python 3.10+, `requests`, `beautifulsoup4`. Optional `curl_cffi`
(install with the `[cloudflare]` extra) gives publisher/Sci-Hub fetches a real
Chrome TLS/JA3 fingerprint, which clears most passive Cloudflare blocks.
macOS only (uses `open -a`).

---

## Architecture

```
endnote_quick_add_tool/
├── pyproject.toml
├── README.md
├── DOCS.md                         (this file)
└── src/endnote_quick_add/
    ├── __init__.py
    ├── cli.py                      ── arg parsing, orchestration, picker
    ├── config.py                   ── ~/.config/endnote_quick_add/config.toml
    ├── resolver.py                 ── input → CrossRefRecord
    ├── pdf_fetcher.py              ── CrossRefRecord → cached PDF
    ├── ris_writer.py               ── CrossRefRecord + PDF → .ris file
    └── endnote.py                  ── open .ris in EndNote.app
```

### Data flow

```
user input ("title" or DOI)
        │
        ▼
   resolver.resolve()        ─── CrossRef API
        │
        ▼
   CrossRefRecord
        │       ╲
        │        ╲
        ▼         ▼
 ris_writer    pdf_fetcher   ─── arXiv / Unpaywall / publisher / Sci-Hub
        │         │
        │         ▼
        │      cached PDF
        ▼         │
   citation.ris ◄─┘  (with L1 file:// link to the PDF)
        │
        ▼
   endnote.import_to_endnote()  ─── open -a "EndNote 21"
        │
        ▼
   EndNote import dialog
```

Each module has one job and no upward dependencies, so any of them can be
tested in isolation and replaced without touching the others.

---

## Module reference

### `resolver.py`

Turns user input into a `CrossRefRecord` (a normalized view of CrossRef's
JSON).

**Public API**

- `is_doi(s: str) -> bool` — recognizes plain DOIs, `doi:` prefix, and
  `https://doi.org/...` URLs.
- `normalize_doi(s: str) -> str` — strips `doi:` and `https://doi.org/`
  prefixes, returning the bare `10.x/y` form.
- `fetch_by_doi(doi, *, email=None, timeout=15.0) -> CrossRefRecord` —
  hits `https://api.crossref.org/works/{doi}`. Raises `LookupError` on 404.
- `search_by_title(title, *, rows=5, email=None) -> list[CrossRefRecord]` —
  hits `https://api.crossref.org/works?query.bibliographic=...`. Returns
  up to `rows` results in CrossRef's relevance order.

**`CrossRefRecord`** captures `doi`, `title`, `authors`, `year`,
`container_title`, `volume`, `issue`, `page`, `publisher`, `type`, `url`,
plus the raw JSON in `.raw` for downstream consumers (e.g. arXiv ID lookup
in `pdf_fetcher`). Convenience: `record.summary()` → human-readable line
for the picker; `record.short_authors` → "Lovelace and Turing", "Harris
et al.", etc.

**Why CrossRef and not Google Scholar / Semantic Scholar?** CrossRef is
free, no-API-key, has a documented "polite pool" you opt into by passing
your email in the User-Agent, and returns rich, structured metadata for
~140M registered DOIs. Title search isn't as smart as Scholar, but the
top-5 picker covers that gap.

### `pdf_fetcher.py`

Tries each PDF source in order and stops at the first that yields a real
PDF (i.e. response body starts with `%PDF`).

**Public API**

- `fetch_pdf(record, *, cache_dir, unpaywall_email, scihub_mirror,
  override_url=None) -> tuple[FetchResult | None, list[str]]` — main
  orchestrator. Returns `(result, attempt_log)` where `attempt_log` is
  the list of `"<source>: ok"` / `"<source>: failed (<reason>)"` lines
  the CLI prints.
- `fetch_pdf_with_handoff(record, *, cache_dir, unpaywall_email,
  scihub_mirror, override_url=None) -> tuple[FetchResult | None, list[str],
  BrowserHandoff | None]` — same source chain, plus a browser handoff URL
  when an automated publisher request hits a Cloudflare challenge.
- `use_local_pdf(local, record, cache_dir) -> FetchResult` — copies a
  user-supplied PDF into the cache and validates the `%PDF` header.
- `try_arxiv`, `try_unpaywall`, `try_publisher`, `try_scihub` — individual
  source functions, each raising on failure. Useful if you want to call a
  single source directly.

**`FetchResult`** is `(pdf_path: Path, source: str)` where `source` is one
of `"arxiv" | "unpaywall" | "publisher" | "scihub" | "manual" | "cache"`.

The orchestrator validates that downloaded bytes start with `%PDF` (catches
HTML error pages disguised as PDFs) and cleans up partial downloads
between source attempts.

**HTTP transport.** `_http_get` dispatches to either `curl_cffi.requests` (when
installed and `USE_CURL_CFFI` is true) or plain `requests`. The `curl_cffi`
path impersonates Chrome at the TLS layer (`IMPERSONATE = "chrome124"`), which
is what clears the common Cloudflare TLS-fingerprint block. Only
publisher/Sci-Hub-facing calls (`_download`, `try_publisher`, `try_scihub`)
route through the dispatcher; the CrossRef and Unpaywall JSON APIs stay on
plain `requests`. Tests pin `USE_CURL_CFFI = False` via `tests/conftest.py` so
`requests_mock` can still intercept everything.

**Browser-cookie reuse.** When `try_publisher` / `try_scihub` are called with
`browser_cookies="chrome"` (or `"safari"`, `"firefox"`, `"edge"`, `"brave"`),
`_load_browser_cookies` uses `browser_cookie3` to extract cookies for the URL's
registered domain (eTLD+1 via `_registered_domain`) from the user's real
browser profile. Those cookies — `cf_clearance`, university SSO session
tokens, etc. — are passed as a dict into `_http_get` and reused for the
follow-up PDF download on the same domain. This is what gets paywalled APS /
Elsevier / ACS articles when TLS impersonation alone falls short. On macOS,
reading Chrome cookies triggers a Keychain prompt the first time; failures
raise `CookieLoadError`, which surfaces in the attempt log just like any
other source failure.

When `_raise_for_status` still detects a Cloudflare challenge after the
TLS-impersonation attempt (cookies, Turnstile, login wall — things curl_cffi
can't solve on its own), the CLI opens the DOI or publisher page in the user's
normal browser for manual university-login access instead of trying to work
around the challenge.

### `ris_writer.py`

Maps a `CrossRefRecord` to RIS format.

**Public API**

- `build_ris(record, pdf_path=None) -> str` — pure function; returns the
  RIS file content as text.
- `write_ris(record, out_dir, pdf_path=None) -> Path` — writes
  `<out_dir>/citation.ris` and returns the path.

The RIS `L1` tag is the magic ingredient: when EndNote imports a record
with `L1  - file:///abs/path.pdf`, it copies the PDF into the library and
links it to the new entry. No AppleScript, no PDF auto-import folder
configuration needed.

### `endnote.py`

A thin shell around `open -a`.

**Public API**

- `import_to_endnote(ris_path, app_name="EndNote 21")` — runs
  `open -a <app_name> <ris_path>`. Raises `EndNoteNotFound` if `open`
  is missing (non-Mac) or returns a non-zero exit code (typically meaning
  the app name is wrong / EndNote isn't installed).

### `config.py`

TOML config loader at `~/.config/endnote_quick_add/config.toml`.

**Public API**

- `load_config(path=CONFIG_PATH) -> Config` — on first run, writes a
  commented template and exits with instructions.

**`Config`** fields: `email`, `endnote_app`, `scihub_mirror`, `cache_dir`,
plus `has_unpaywall` and `has_scihub` predicates that the orchestrator
uses to decide whether to include those sources in the chain.

### `cli.py`

Glue: parses args, calls `resolver`, runs the picker (for title input),
calls `pdf_fetcher`, calls `ris_writer`, calls `endnote.import_to_endnote`.
Prints status at each step. All user interaction (the picker) lives here,
so the rest of the modules are I/O-pure and easy to test.

---

## Configuration

`~/.config/endnote_quick_add/config.toml`:

```toml
email = "you@example.com"          # required by Unpaywall API
endnote_app = "EndNote 21"         # passed to `open -a`
scihub_mirror = ""                 # optional last-resort mirror, e.g. "https://sci-hub.se"
cache_dir = "~/.cache/endnote_quick_add"
```

| Key | Behavior if missing/default |
|---|---|
| `email` left as `you@example.com` | Unpaywall is skipped from the source chain. |
| `scihub_mirror` empty string | Sci-Hub is skipped from the source chain. |
| `endnote_app` | Defaults to `"EndNote 21"`. Override per-run with `--app`. |
| `cache_dir` | Defaults to `~/.cache/endnote_quick_add`. |

The first run writes this template and tells you to fill it in before
running again.

---

## PDF source chain

In order:

| # | Source | Trigger | How it works |
|---|---|---|---|
| 1 | arXiv | DOI matches `10.48550/arXiv.*` or `relation` field references arXiv | `GET arxiv.org/pdf/{id}.pdf` |
| 2 | Unpaywall | `email` configured | `GET api.unpaywall.org/v2/{doi}?email=...`, follow `best_oa_location.url_for_pdf` |
| 3 | Publisher | record has a URL | `GET record.url`; if response is `application/pdf`, save it. Otherwise scrape `<meta name="citation_pdf_url">` (the standard Highwire Press meta tag almost every publisher emits) and follow that link. |
| 4 | Sci-Hub | `scihub_mirror` configured | `GET {mirror}/{doi}`, parse `<embed>` or `<iframe>` for the PDF URL. |

Each attempt:

- Validates the response body starts with `%PDF` before saving.
- Falls through to the next source on any exception.
- Logs success/failure to a list that the CLI prints, so you can see why
  each source did or didn't work.

The chain short-circuits on the first success. If all sources fail the
RIS file is still written (without `L1`) and EndNote still gets a clean
citation import — you just have to attach the PDF manually.

**Override.** `--pdf URL` or `--pdf /path/to/local.pdf` skips the entire
chain.

**Cache.** Once a DOI's PDF is in `cache_dir`, subsequent runs short-circuit
before trying any source.

### Why these sources, in this order

- **arXiv first** because it's the cleanest source: no paywall, no
  scraping, deterministic URL, fast.
- **Unpaywall second** because it's the right way to find legal open-access
  copies (institutional repos, PMC, etc.) without scraping.
- **Publisher third** because the user is on always-on university VPN, so
  publisher landing pages resolve directly. The `citation_pdf_url` meta
  tag is a near-universal standard among journal publishers.
- **Sci-Hub last** as an explicit, opt-in fallback.

---

## CrossRef → RIS field mapping

| CrossRef field | RIS tag | Notes |
|---|---|---|
| `type` | `TY` | Mapped via `CROSSREF_TO_RIS_TYPE` (e.g. `journal-article`→`JOUR`, `book-chapter`→`CHAP`). Defaults to `JOUR`. |
| `author[].given` + `family` | `AU` | One `AU` line per author, formatted `Family, Given`. |
| `title[0]` | `TI` | |
| `container-title[0]` | `T2` and `JF` | Both emitted for compatibility. |
| `volume` | `VL` | |
| `issue` | `IS` | |
| `page` | `SP` / `EP` | Split on `-`. |
| `issued.date-parts[0][0]` (with fallbacks to `published-print`, `published-online`, `created`) | `PY` | First non-empty year wins. |
| `DOI` | `DO` | |
| `URL` | `UR` | |
| `publisher` | `PB` | |
| `(local PDF path)` | `L1` | `file://<absolute-path>`. Causes EndNote to attach the PDF on import. |
| — | `ER` | Required terminator. |

Type mappings live in
[`ris_writer.py`](src/endnote_quick_add/ris_writer.py:8) — extend the
`CROSSREF_TO_RIS_TYPE` dict to cover more cases.

---

## Caching

```
~/.cache/endnote_quick_add/
└── 10.1038_s41586-020-2649-2/        ← DOI slug (slashes → underscores)
    ├── paper.pdf                     ← downloaded PDF
    └── citation.ris                  ← generated RIS, points at paper.pdf
```

- Cache key: DOI with non-`[\w.-]` characters replaced by `_`.
- Cache hit logic: if `paper.pdf` exists, has nonzero size, and starts with
  `%PDF`, skip the source chain entirely.
- No expiration. Delete the directory to force a re-fetch.
- Manual PDFs (`--pdf /path/to.pdf`) are copied into the same layout so
  subsequent runs see them as cached.

---

## CLI reference

```
eqa <query> [options]
```

| Option | Effect |
|---|---|
| `<query>` | DOI or paper title (free text). Multiple positional words are joined. |
| `--pdf URL_OR_PATH` | Use this PDF instead of the source chain. URL or absolute/relative local path. |
| `--no-pdf` | Skip the PDF step entirely; import a citation-only RIS. |
| `--app "EndNote N"` | Override the EndNote app name from config. |
| `--dry-run` | Resolve, fetch, write RIS — but don't open EndNote. Useful for inspecting the cache outputs. |
| `--config PATH` | Use a non-default config file (handy for testing). |
| `-h, --help` | Show argparse help. |

**Exit codes:** `0` success (including "no PDF available, citation only"),
`1` no CrossRef hit / bad input, `2` EndNote launch failure.

### Examples

```bash
# Fast path: by DOI.
eqa 10.1038/s41586-020-2649-2

# By title, with picker.
eqa "attention is all you need"

# I already have the PDF.
eqa --pdf ~/Downloads/paper.pdf 10.1038/s41586-020-2649-2

# Citation only (forgot to attach PDF? Re-run later without --no-pdf to add it.)
eqa --no-pdf 10.1038/s41586-020-2649-2

# Dry run to inspect what would be imported.
eqa --dry-run "attention is all you need"
```

---

## Testing

```bash
pip install -e ".[dev]"
pytest
```

21 tests across three files, all using `requests-mock` (no live network):

- `tests/test_resolver.py` — DOI detection/normalization, CrossRef
  response mapping, search-by-title.
- `tests/test_pdf_fetcher.py` — arXiv ID extraction (DOI, `relation`
  field), source-chain ordering and short-circuiting, publisher meta-tag
  scraping, all-sources-fail behavior, cache hit behavior, manual local
  PDF copy and validation.
- `tests/test_ris_writer.py` — field mapping snapshot, `L1` PDF
  attachment, RIS type mapping per CrossRef type.

The CLI itself isn't unit-tested — its job is glue, and all the
substantive logic lives in modules that are tested directly. A live
end-to-end smoke test against CrossRef is recommended after dependency
upgrades:

```bash
eqa --dry-run --no-pdf 10.1038/s41586-020-2649-2
```

---

## Extending

### Adding a new PDF source

1. Write `try_<source>(record, dest, *, ...) -> bool` in `pdf_fetcher.py`.
   Raise on failure (any exception type with a useful message — it ends up
   in the user-visible attempt log). On success, leave a valid PDF at
   `dest`.
2. Slot it into the `sources` list in `fetch_pdf_with_handoff` at the right priority
   level. Wrap with a config gate if needed (e.g. `if config.has_X:`).
3. Add a test mocking the new source's HTTP endpoints; for chain ordering,
   verify it does/doesn't fire when expected sources up-stream succeed.

### Adding a new RIS type mapping

Append to `CROSSREF_TO_RIS_TYPE` in `ris_writer.py`. Add a test mirroring
`test_book_chapter_maps_to_chap`.

### Replacing the metadata source

`resolver.py` is the only module that knows about CrossRef. To swap to
OpenAlex, Semantic Scholar, etc., reimplement `fetch_by_doi` /
`search_by_title` to return `CrossRefRecord` (or rename the class — the
shape is what matters; downstream modules only read fields, not the
provider).

---

## Limitations and known issues

- **macOS only.** `open -a` is the import mechanism. Linux/Windows would
  need a different bridge to a reference manager.
- **arXiv-only DOIs aren't always in CrossRef.** The `10.48550/arXiv.*`
  prefix coverage is patchy, especially for older papers. Workaround:
  search by title — the top hit is typically the published version (which
  CrossRef indexes) or an indexed preprint mirror, and the arXiv source
  still kicks in for the PDF.
- **Title search relevance.** CrossRef's `query.bibliographic` is good
  but not Scholar-quality. The top-5 picker is the safety net.
- **No bulk mode.** One paper per invocation by design. A `--batch` flag
  reading DOIs from stdin would be a natural extension.
- **No deduplication against the EndNote library.** EndNote's own import
  dialog handles duplicates, so this is intentional.
- **`L1` requires absolute paths.** RIS-relative paths confuse EndNote.
  The writer always emits `file://<absolute>`.
- **Sci-Hub mirror DNS / structure changes** can break `try_scihub`. It
  currently parses `<embed>` and `<iframe>` tags; if a mirror changes
  layout, the source will fail and fall through (or, more likely, be the
  only failing source after everything else has been skipped).
