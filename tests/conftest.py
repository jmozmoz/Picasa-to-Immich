"""Shared fixtures: an isolated environment, Picasa trees and a fake Immich server."""
from __future__ import annotations

import base64
import copy
import hashlib
from pathlib import Path
from typing import Any

import pytest

import picasa_to_immich as p2i


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch, tmp_path_factory):
    """Never read the developer's real .env or IMMICH_* variables in tests."""
    monkeypatch.delenv("IMMICH_URL", raising=False)
    monkeypatch.delenv("IMMICH_API_KEY", raising=False)
    monkeypatch.setattr(p2i, "DEFAULT_ENV_FILE", tmp_path_factory.mktemp("noenv") / ".env")


def b64_sha1(data: bytes) -> str:
    return base64.b64encode(hashlib.sha1(data).digest()).decode("ascii")


def external_asset(asset_id: str, name: str, original_path: str, size: int) -> dict[str, Any]:
    """An external-library asset: Immich stores SHA-1("path:" + originalPath) as checksum."""
    return {"id": asset_id, "originalFileName": name, "originalPath": original_path,
            "checksum": b64_sha1(f"path:{original_path}".encode()), "exifInfo": {"fileSizeInByte": size}}


def uploaded_asset(asset_id: str, name: str, content: bytes) -> dict[str, Any]:
    return {"id": asset_id, "originalFileName": name, "originalPath": f"/upload/{name}",
            "checksum": b64_sha1(content)}


def write(path: Path, text: str | bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(text, bytes):
        path.write_bytes(text)
    else:
        path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def picasa_tree(tmp_path) -> Path:
    """A small Picasa collection with two folders, three albums and a few edge cases."""
    root = tmp_path / "pictures"
    write(root / "2020" / "a.jpg", b"content-a")
    write(root / "2020" / "b.jpg", b"content-b")
    write(root / "2020" / "sub" / "a.jpg", b"content-sub-a")
    write(root / "2021" / "v.mp4", b"content-v")
    write(root / "2021" / "notes.txt", b"not an image")
    write(root / "2020" / ".picasa.ini", (
        "[Picasa]\nname=Holiday folder\ndate=43831.980718\n"
        "[.album:tok1]\nname=Holiday 2020\ntoken=tok1\n"
        "[.album:tok2]\nname=Best of\n"
        "[a.jpg]\nalbums=tok1,tok2\nrotate=rotate(1)\n"
        "[b.jpg]\nalbums=tok1, tokX\n"
        "[missing.jpg]\nalbums=tok1\n"
    ))
    write(root / "2020" / "sub" / ".picasa.ini", "[a.jpg]\nalbums=tok2\n")
    write(root / "2021" / ".picasa.ini", (
        "[.album:tok3]\ndate=2021-01-01\n"
        "[v.mp4]\nalbums=tok3,tok1\n"
        "[notes.txt]\nalbums=tok3\n"
    ))
    return root


class FakeImmich:
    """In-memory stand-in for ImmichAPI that records every change request."""

    def __init__(self, assets=(), albums=(), members=None, failing_albums=()):
        self.assets = list(assets)
        self.albums = list(albums)
        self.members = {k: set(v) for k, v in (members or {}).items()}
        self.failing_albums = set(failing_albums)
        self.created: list[tuple[str, list[str]]] = []
        self.added: list[tuple[str, list[str]]] = []

    def server_version(self):
        return "fake"

    def list_assets(self, page_size=1000):
        return copy.deepcopy(self.assets)

    def list_albums(self):
        return copy.deepcopy(self.albums)

    def album_asset_ids(self, album_id, page_size=1000):
        if album_id in self.failing_albums:
            raise RuntimeError("read failed")
        return set(self.members.get(album_id, set()))

    def create_album(self, name, asset_ids):
        self.created.append((name, list(asset_ids)))
        return {"id": f"new-{len(self.created)}"}

    def add_album_assets(self, album_id, asset_ids):
        self.added.append((album_id, list(asset_ids)))
