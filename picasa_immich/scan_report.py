"""Scan-only mode: Picasa album membership as source-relative file paths."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Iterable, Mapping

from .picasa import PicasaLibrary
from .util import json_write, timestamp

LOG = logging.getLogger(__name__)


def build_album_file_list(library: PicasaLibrary, local_paths: Mapping[str, Path]) -> dict[str, Any]:
    """The content of album_file_list.json: each album with its existing and missing files."""
    albums: list[dict[str, Any]] = []
    for token in library.albums:
        files: set[str] = set()
        missing: set[str] = set()
        for key, record in library.files.items():
            if token in record["albumTokens"]:
                (files if key in local_paths else missing).add(record["relativePath"])
        albums.append(
            {
                "token": token,
                "name": library.album_name(token),
                "files": sorted(files, key=str.casefold),
                "missingFiles": sorted(missing, key=str.casefold),
                "fileCount": len(files),
            }
        )
    return {
        "createdAt": timestamp(),
        "source": str(library.root),
        "iniFileCount": len(library.ini_files),
        "albumCount": len(albums),
        "folderMetadataCount": len(library.folders),
        "imageSections": len(library.files),
        "folders": sorted(library.folders.values(), key=lambda item: item["relativePath"].casefold()),
        "albums": albums,
        "warnings": list(library.warnings),
    }


def album_file_list_text(albums: Iterable[Mapping[str, Any]]) -> str:
    """Plain-text version: one album heading followed by its relative paths."""
    lines: list[str] = []
    for album in albums:
        lines.append(f"[{album['name']}] (token: {album['token']})")
        lines.extend(album["files"])
        if album["missingFiles"]:
            lines.append("# Missing or unsupported files:")
            lines.extend(f"# {name}" for name in album["missingFiles"])
        lines.append("")
    return "\n".join(lines)


def run_scan_only(root: Path, output: Path) -> dict[str, Any]:
    """Scan root and write album_file_list.json and album_file_list.txt to output."""
    library = PicasaLibrary.scan(root)
    result = build_album_file_list(library, library.local_files())
    json_write(output / "album_file_list.json", result)
    output.mkdir(parents=True, exist_ok=True)
    (output / "album_file_list.txt").write_text(album_file_list_text(result["albums"]), encoding="utf-8")
    LOG.info("Wrote %d albums to %s", result["albumCount"], output)
    LOG.info("Album file lists: %s and %s", output / "album_file_list.json", output / "album_file_list.txt")
    return result
