"""Immich URL and API key from the command line, the environment or a .env file.

Without --env-file, the first existing file of default_env_files() is used:
  1. .env in the current working directory
  2. .env in the user configuration directory (see user_config_dir)
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from dotenv import dotenv_values

APP_NAME = "picasa-to-immich"
ENV_FILE_NAME = ".env"


def user_config_dir(
    environ: Mapping[str, str] = os.environ, platform: str = sys.platform, home: Path | None = None
) -> Path:
    """Per-user configuration directory for this program.

    Windows: %APPDATA%\\picasa-to-immich
    macOS:   ~/Library/Application Support/picasa-to-immich
    Other:   $XDG_CONFIG_HOME/picasa-to-immich, default ~/.config/picasa-to-immich
    """
    home = home or Path.home()
    if platform == "win32":
        base = Path(environ["APPDATA"]) if environ.get("APPDATA") else home / "AppData" / "Roaming"
    elif platform == "darwin":
        base = home / "Library" / "Application Support"
    else:
        base = Path(environ["XDG_CONFIG_HOME"]) if environ.get("XDG_CONFIG_HOME") else home / ".config"
    return base / APP_NAME


def default_env_files() -> list[Path]:
    """Places searched for a .env file when --env-file is not given, in order."""
    return [Path.cwd() / ENV_FILE_NAME, user_config_dir() / ENV_FILE_NAME]


def read_env_file(path: Path) -> dict[str, str]:
    """Read KEY=VALUE lines from a .env file with python-dotenv; return {} if the file does not exist.

    Uses the common .env syntax: '#' comments (also after a value), an optional
    'export ' prefix, quoted values with escapes and multi-line quoted values.
    ${VAR} references are not expanded. Keys without '=' are ignored, and
    python-dotenv logs a warning for lines it cannot parse and skips them.
    """
    if not path.is_file():
        return {}
    values = dotenv_values(path, interpolate=False, encoding="utf-8-sig")
    return {key: value for key, value in values.items() if value is not None}


@dataclass
class Credentials:
    url: str | None
    api_key: str | None
    env_file_used: Path | None = None


def find_env_file(env_file: Path | None) -> Path | None:
    """The explicit file (which must exist), else the first existing default file, else None."""
    if env_file:
        explicit = env_file.expanduser()
        if not explicit.is_file():
            raise ValueError(f"--env-file not found: {explicit}")
        return explicit
    return next((p for p in default_env_files() if p.is_file()), None)


def resolve_credentials(
    url: str | None, api_key: str | None, env_file: Path | None, environ: Mapping[str, str] = os.environ
) -> Credentials:
    """Combine settings with priority: command line, environment, .env file.

    `env_file` is an explicitly requested file (it must exist) or None to use
    the first existing file of default_env_files(), if any. Only one file is read.
    Raises ValueError if an explicit file is missing or a file cannot be decoded.
    """
    path = find_env_file(env_file)
    file_values: dict[str, str] = {}
    if path:
        try:
            file_values = read_env_file(path)
        except (OSError, ValueError) as exc:
            raise ValueError(f"Cannot read {path}: {exc}") from exc
    return Credentials(
        url=url or environ.get("IMMICH_URL") or file_values.get("IMMICH_URL"),
        api_key=api_key or environ.get("IMMICH_API_KEY") or file_values.get("IMMICH_API_KEY"),
        env_file_used=path if file_values else None,
    )
