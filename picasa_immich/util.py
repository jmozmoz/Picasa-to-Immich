"""Small helpers shared by several modules."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any


def json_write(path: Path, data: Any) -> None:
    """Write JSON atomically (via a temporary file), with sorted keys."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def new_run_dir(output: Path, mode: str, now: float | None = None) -> Path:
    """Create and return a new, empty folder output/<YYYY-MM-DDTHHMMSS>-<mode> for one run.

    An existing folder is never reused, so earlier reports are never overwritten:
    a second run in the same second gets the suffix "-2", then "-3", and so on.
    """
    output.mkdir(parents=True, exist_ok=True)
    base = f"{time.strftime('%Y-%m-%dT%H%M%S', time.localtime(now))}-{mode}"
    number = 1
    while True:
        path = output / (base if number == 1 else f"{base}-{number}")
        try:
            path.mkdir()
            return path
        except FileExistsError:
            number += 1


def norm_path(value: str | Path) -> str:
    """Case-insensitive, platform-independent key for a path relative to source."""
    return str(value).replace("\\\\", "/").replace("\\", "/").casefold()
