"""Matching local files to Immich assets. Pure logic, no network access."""
from __future__ import annotations

import base64
import hashlib
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping


def sha1_file(path: Path, chunk_size: int = 4 * 1024 * 1024) -> tuple[str, int]:
    """Return (hex SHA-1, size in bytes) of a file."""
    digest = hashlib.sha1()
    size = 0
    with path.open("rb") as stream:
        while chunk := stream.read(chunk_size):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def checksum_forms(hex_digest: str) -> set[str]:
    """A SHA-1 as lowercase hex and as base64 (the form Immich returns)."""
    return {hex_digest.lower(), base64.b64encode(bytes.fromhex(hex_digest)).decode("ascii")}


def remote_size(asset: Mapping[str, Any]) -> int | None:
    for key in ("fileSize", "originalFileSize", "size"):
        value = asset.get(key)
        if isinstance(value, (int, float)):
            return int(value)
    exif = asset.get("exifInfo")
    if isinstance(exif, dict):
        # Immich's ExifResponseDto names this field "fileSizeInByte".
        for key in ("fileSizeInByte", "fileSize", "fileSizeInBytes"):
            value = exif.get(key)
            if isinstance(value, (int, float)):
                return int(value)
    return None


def is_path_based_checksum(asset: Mapping[str, Any]) -> bool:
    """True if the server checksum is SHA-1("path:" + originalPath), not a content hash.

    Immich stores this placeholder for external-library assets, so it can never
    equal the SHA-1 of the local file.
    """
    value = asset.get("checksum")
    original_path = asset.get("originalPath")
    if not (isinstance(value, str) and value and isinstance(original_path, str) and original_path):
        return False
    path_sha1 = hashlib.sha1(f"path:{original_path}".encode("utf-8")).hexdigest()
    return value.strip() in checksum_forms(path_sha1)


def remote_checksum(asset: Mapping[str, Any]) -> str | None:
    """Return the server's content checksum, or None if it is unknown or path-based."""
    value = asset.get("checksum")
    if isinstance(value, str) and value and not is_path_based_checksum(asset):
        return value.strip()
    return None


class AssetIndex:
    """Immich assets indexed by lowercase filename and by content checksum.

    Lookups return assets in their original inventory order.
    """

    def __init__(self, assets: Iterable[dict[str, Any]]):
        self.assets = list(assets)
        self.by_name: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self.by_checksum: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self._checksum_of: dict[int, str | None] = {}
        self._position: dict[int, int] = {}
        for position, asset in enumerate(self.assets):
            checksum = remote_checksum(asset)
            self._position[id(asset)] = position
            self._checksum_of[id(asset)] = checksum
            self.by_name[str(asset.get("originalFileName", "")).casefold()].append(asset)
            if checksum is not None:
                self.by_checksum[checksum].append(asset)

    def __len__(self) -> int:
        return len(self.assets)

    def with_name(self, filename: str) -> list[dict[str, Any]]:
        return list(self.by_name.get(filename.casefold(), []))

    def with_checksum(self, checksums: Iterable[str]) -> list[dict[str, Any]]:
        found = {id(a): a for c in checksums for a in self.by_checksum.get(c, [])}
        return sorted(found.values(), key=lambda a: self._position[id(a)])

    def content_checksum(self, asset: Mapping[str, Any]) -> str | None:
        return self._checksum_of[id(asset)]


@dataclass
class Match:
    status: str  # matched | matched_low_confidence | ambiguous | unmatched
    asset: dict[str, Any] | None = None
    candidates: list[dict[str, Any]] = field(default_factory=list)
    reason: str = ""


def match_asset(local_sha1: str, local_size: int, filename: str, index: AssetIndex, allow_name_only: bool) -> Match:
    """Match one local file (by its SHA-1, size and name) against the Immich inventory."""
    local_checksums = checksum_forms(local_sha1)
    # SHA-1 is authoritative when the server provides a content checksum.
    sha_matches = index.with_checksum(local_checksums)
    if len(sha_matches) == 1:
        return Match("matched", sha_matches[0], reason="sha1")
    if len(sha_matches) > 1:
        return Match("ambiguous", candidates=sha_matches, reason="multiple_sha1_matches")

    same_name = index.with_name(filename)
    # A same-name asset with a different known checksum is rejected, even if its size matches.
    checksum_mismatch = [a for a in same_name if index.content_checksum(a) is not None]
    if checksum_mismatch:
        return Match("unmatched", candidates=checksum_mismatch, reason="server_checksum_mismatch")

    size_matches = [a for a in same_name if remote_size(a) == local_size]
    if len(size_matches) == 1:
        reason = ("filename_and_size_path_based_server_checksum" if is_path_based_checksum(size_matches[0])
                  else "filename_and_size_no_server_checksum")
        return Match("matched", size_matches[0], reason=reason)
    if len(size_matches) > 1:
        return Match("ambiguous", candidates=size_matches, reason="multiple_filename_size_matches")

    if allow_name_only and same_name:
        if len(same_name) == 1:
            return Match("matched_low_confidence", same_name[0], reason="filename_only_explicitly_allowed")
        return Match("ambiguous", candidates=same_name, reason="multiple_filename_matches")
    if same_name:
        return Match("unmatched", candidates=same_name, reason="filename_found_but_size_unavailable_or_different")
    return Match("unmatched", reason="no_candidate")


def match_local_file(path: Path, index: AssetIndex, allow_name_only: bool) -> Match:
    try:
        local_sha1, local_size = sha1_file(path)
    except OSError as exc:
        return Match("unmatched", reason=f"cannot_read_local_file: {exc}")
    return match_asset(local_sha1, local_size, path.name, index, allow_name_only)
