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

To bypass most Cloudflare TLS-fingerprint blocks on publisher sites
(`journals.aps.org`, `linkinghub.elsevier.com`, etc.), install the optional
`cloudflare` extra. It pulls in [`curl_cffi`](https://github.com/lexiforest/curl_cffi),
which makes publisher requests using a real Chrome TLS/JA3 fingerprint:

```bash
pipx install -e '/path/to/endnote_quick_add_tool[cloudflare]'
# or, if already installed:
pipx inject endnote-quick-add curl_cffi
```

When `curl_cffi` is importable, `eqa` uses it automatically for publisher and
Sci-Hub fetches; arXiv and Unpaywall keep using plain `requests`. Hard
challenges (interactive Turnstile, login walls) still fall back to the browser
handoff described below.

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

If a publisher blocks the automated PDF request with a Cloudflare challenge,
`eqa` opens the DOI/publisher page in your normal browser. Log in through your
university access there, download the PDF manually, then rerun with
`--pdf ~/Downloads/paper.pdf` to attach it.

### Reusing your browser session (recommended for paywalled journals)

For sites where TLS impersonation alone isn't enough — Elsevier, ACS, Wiley,
Springer, IEEE — `eqa` can pull cookies from your real, logged-in browser and
replay them on the publisher request. This carries both your `cf_clearance`
(the Cloudflare-issued bypass cookie) and your university-SSO session, so the
same stuff that works in your browser works headlessly:

```bash
eqa --browser-cookies chrome 10.1016/j.cell.2024.01.001
```

Or set it as the default in `~/.config/endnote_quick_add/config.toml`:

```toml
browser_cookies = "chrome"   # also: "safari", "firefox", "edge", "brave"
```

The first time `eqa` reads Chrome cookies on macOS, the system asks Keychain to
release "Chrome Safe Storage" — click **Always Allow** to skip the prompt on
later runs. Cookies are scoped to the publisher's registered domain (e.g.
`aps.org`) so unrelated cookies are not sent.

#### What this does and doesn't clear

| Publisher pattern                    | TLS impersonation | + browser cookies | Result        |
|--------------------------------------|-------------------|-------------------|---------------|
| arXiv, Unpaywall, OA mirrors         | n/a               | n/a               | always works  |
| Most Cloudflare-fronted publishers   | clears            | clears            | works         |
| Login-walled (Elsevier/ACS/Wiley/…)  | blocks            | clears            | works with cookies |
| **APS journals (`journals.aps.org`)**| blocks            | **still blocks**  | falls back to browser handoff |

APS runs Cloudflare's strictest "managed challenge" mode, which regenerates a
short-lived `__cf_bm` cookie on every JS challenge solve. That cookie lives
only in the browser's in-memory store (often as a partitioned cookie), so
`browser_cookie3` can't extract it and replay alone won't satisfy Cloudflare.
For APS the realistic flow is: let `eqa` open the article in your browser
(automatic on challenge), download the PDF manually, then attach with `--pdf`.

## How it works

1. **Resolve** the input via the CrossRef API. DOI → direct lookup; title →
   top-5 picker.
2. **Fetch the PDF** by trying sources in order, stopping at the first hit:
   - **arXiv** if the record points to a preprint (e.g. `10.48550/arXiv.*`).
   - **Unpaywall** for open-access copies.
   - **Publisher URL** directly (works on always-on VPN / on-campus). The tool
     looks for `<meta name="citation_pdf_url">` on the landing page.
   - **Sci-Hub** mirror (optional, configured in `scihub_mirror`).

   Publisher and Sci-Hub fetches use `curl_cffi` (when installed) to impersonate
   a real Chrome's TLS fingerprint, which clears most passive Cloudflare blocks.
   If a Cloudflare challenge still comes back (cookies, Turnstile, login wall),
   `eqa` opens the article URL in your browser for manual login/download.
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
