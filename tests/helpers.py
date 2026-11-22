"""Helpers shared by the tests: fake Immich assets, a fake Immich server and file writing."""

from __future__ import annotations

import base64
import copy
import hashlib
from pathlib import Path
from typing import Any


def b64_sha1(data: bytes) -> str:
    return base64.b64encode(hashlib.sha1(data).digest()).decode("ascii")


def external_asset(asset_id: str, name: str, original_path: str, size: int) -> dict[str, Any]:
    """An external-library asset: Immich stores SHA-1("path:" + originalPath) as checksum."""
    return {
        "id": asset_id,
        "originalFileName": name,
        "originalPath": original_path,
        "checksum": b64_sha1(f"path:{original_path}".encode()),
        "exifInfo": {"fileSizeInByte": size},
    }


def uploaded_asset(asset_id: str, name: str, content: bytes) -> dict[str, Any]:
    return {"id": asset_id, "originalFileName": name, "originalPath": f"/upload/{name}", "checksum": b64_sha1(content)}


def only_run_dir(output: Path, mode: str) -> Path:
    """The single run folder of the given mode that the CLI created in output."""
    (run_dir,) = output.glob(f"*-{mode}")
    return run_dir


def write(path: Path, text: str | bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(text, bytes):
        path.write_bytes(text)
    else:
        path.write_text(text, encoding="utf-8")
    return path


class FakeImmich:
    """In-memory stand-in for ImmichAPI that records every change request."""

    def __init__(self, assets=(), albums=(), members=None, failing_albums=()):
        self.assets = list(assets)
        self.albums = list(albums)
        self.members = {k: set(v) for k, v in (members or {}).items()}
        self.failing_albums = set(failing_albums)
        self.created: list[tuple[str, list[str]]] = []
        self.added: list[tuple[str, list[str]]] = []
        self.descriptions: dict[str, str] = {}  # album name -> description given on creation

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

    def create_album(self, name, asset_ids, description=""):
        self.created.append((name, list(asset_ids)))
        self.descriptions[name] = description
        return {"id": f"new-{len(self.created)}"}

    def add_album_assets(self, album_id, asset_ids):
        self.added.append((album_id, list(asset_ids)))
