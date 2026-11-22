"""Immich URL and API key from the command line, the environment or a .env file."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

# The project folder, i.e. next to picasa_to_immich.py (not inside this package).
DEFAULT_ENV_FILE = Path(__file__).resolve().parent.parent / ".env"


def read_env_file(path: Path) -> dict[str, str]:
    """Read KEY=VALUE lines from a .env file; return {} if the file does not exist.

    Supports blank lines, '#' comments, an optional 'export ' prefix and values
    in single or double quotes. Values are not expanded or interpolated.
    """
    if not path.is_file():
        return {}
    values: dict[str, str] = {}
    for number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not sep or not key:
            raise ValueError(f"line {number}: expected KEY=VALUE")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


@dataclass
class Credentials:
    url: str | None
    api_key: str | None
    env_file_used: Path | None = None


def resolve_credentials(url: str | None, api_key: str | None, env_file: Path | None,
                        environ: Mapping[str, str] = os.environ) -> Credentials:
    """Combine settings with priority: command line, environment, .env file.

    `env_file` is an explicitly requested file (it must exist) or None for the
    default .env in the project folder (used only if present).
    Raises ValueError if an explicit file is missing or a file is malformed.
    """
    path = (env_file or DEFAULT_ENV_FILE).expanduser()
    if env_file and not path.is_file():
        raise ValueError(f"--env-file not found: {path}")
    try:
        file_values = read_env_file(path)
    except (OSError, ValueError) as exc:
        raise ValueError(f"Cannot read {path}: {exc}") from exc
    return Credentials(
        url=url or environ.get("IMMICH_URL") or file_values.get("IMMICH_URL"),
        api_key=api_key or environ.get("IMMICH_API_KEY") or file_values.get("IMMICH_API_KEY"),
        env_file_used=path if file_values else None,
    )
