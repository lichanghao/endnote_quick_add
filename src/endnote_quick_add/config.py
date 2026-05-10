from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

CONFIG_PATH = Path(os.path.expanduser("~/.config/endnote_quick_add/config.toml"))
DEFAULT_CACHE_DIR = Path(os.path.expanduser("~/.cache/endnote_quick_add"))

TEMPLATE = """\
# endnote_quick_add config
# Edit `email` to a real address (Unpaywall API requires it).

email = "you@example.com"
endnote_app = "EndNote 21"

# Optional: a Sci-Hub mirror used as a last-resort PDF source.
# Leave blank or remove to disable.
scihub_mirror = ""

# Optional: reuse cookies from your real browser when fetching from publishers
# behind Cloudflare/login walls (e.g. journals.aps.org). Requires the
# [cloudflare] extra. Supported: "chrome", "safari", "firefox", "edge", "brave".
# Leave blank to disable. First Chrome read on macOS triggers a Keychain prompt.
browser_cookies = ""

# Where downloaded PDFs and generated RIS files are cached.
cache_dir = "~/.cache/endnote_quick_add"
"""


@dataclass(frozen=True)
class Config:
    email: str
    endnote_app: str
    scihub_mirror: str
    browser_cookies: str
    cache_dir: Path

    @property
    def has_unpaywall(self) -> bool:
        return bool(self.email) and self.email != "you@example.com"

    @property
    def has_scihub(self) -> bool:
        return bool(self.scihub_mirror)


def load_config(path: Path = CONFIG_PATH) -> Config:
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(TEMPLATE)
        raise SystemExit(
            f"Created a config template at {path}.\n"
            f"Please edit it (set your email at minimum) and re-run."
        )

    with path.open("rb") as f:
        data = tomllib.load(f)

    cache_dir = Path(os.path.expanduser(data.get("cache_dir", str(DEFAULT_CACHE_DIR))))
    cache_dir.mkdir(parents=True, exist_ok=True)

    return Config(
        email=data.get("email", ""),
        endnote_app=data.get("endnote_app", "EndNote 21"),
        scihub_mirror=data.get("scihub_mirror", ""),
        browser_cookies=data.get("browser_cookies", ""),
        cache_dir=cache_dir,
    )
