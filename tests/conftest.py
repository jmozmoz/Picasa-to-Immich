"""Shared fixtures: an isolated environment and a small Picasa tree (helpers are in tests/helpers.py)."""

from __future__ import annotations

from pathlib import Path

import pytest

from picasa_immich import settings
from tests.helpers import write


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch, tmp_path_factory):
    """Never read the developer's real .env or IMMICH_* variables in tests."""
    monkeypatch.delenv("IMMICH_URL", raising=False)
    monkeypatch.delenv("IMMICH_API_KEY", raising=False)
    monkeypatch.setattr(settings, "default_env_files", lambda: [tmp_path_factory.mktemp("noenv") / ".env"])


@pytest.fixture
def picasa_tree(tmp_path: Path) -> Path:
    """A small Picasa collection with two folders, three albums and a few edge cases."""
    root = tmp_path / "pictures"
    write(root / "2020" / "a.jpg", b"content-a")
    write(root / "2020" / "b.jpg", b"content-b")
    write(root / "2020" / "sub" / "a.jpg", b"content-sub-a")
    write(root / "2021" / "v.mp4", b"content-v")
    write(root / "2021" / "notes.txt", b"not an image")
    write(
        root / "2020" / ".picasa.ini",
        (
            "[Picasa]\nname=Holiday folder\ndate=43831.980718\n"
            "[.album:tok1]\nname=Holiday 2020\ntoken=tok1\n"
            "[.album:tok2]\nname=Best of\n"
            "[a.jpg]\nalbums=tok1,tok2\nrotate=rotate(1)\n"
            "[b.jpg]\nalbums=tok1, tokX\n"
            "[missing.jpg]\nalbums=tok1\n"
        ),
    )
    write(root / "2020" / "sub" / ".picasa.ini", "[a.jpg]\nalbums=tok2\n")
    write(
        root / "2021" / ".picasa.ini",
        "[.album:tok3]\ndate=2021-01-01\n[v.mp4]\nalbums=tok3,tok1\n[notes.txt]\nalbums=tok3\n",
    )
    return root
