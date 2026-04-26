from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


class EndNoteNotFound(RuntimeError):
    pass


def import_to_endnote(ris_path: Path, app_name: str = "EndNote 21") -> None:
    if shutil.which("open") is None:
        raise EndNoteNotFound("`open` command not found — this tool only runs on macOS.")

    result = subprocess.run(
        ["open", "-a", app_name, str(ris_path)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        # `open -a` returns exit 1 with a message like
        # "Unable to find application named '<name>'".
        raise EndNoteNotFound(
            f"Failed to open EndNote (`{app_name}`): {result.stderr.strip() or result.stdout.strip()}"
        )
