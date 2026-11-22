#!/usr/bin/env python3
"""Scan Picasa albums or migrate their memberships to Immich.

Use --scan-only to export album membership as source-relative file paths; this
mode does not connect to Immich and needs no API key or third-party packages.
Otherwise, dry-run is the default. Use --apply to create albums / add existing
assets. Immich mode requires: pip install requests

The original .picasa.ini files are copied byte-for-byte to OUTPUT/original_ini.
All Picasa properties (including unknown ones) are archived in JSON.
"""
from __future__ import annotations

import argparse
import base64
import configparser
import hashlib
import json
import logging
import os
import shutil
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Protocol

LOG = logging.getLogger("picasa_to_immich")

IMAGE_VIDEO_EXTENSIONS = {
    ".jpg", ".jpeg", ".jpe", ".png", ".gif", ".bmp", ".tif", ".tiff",
    ".webp", ".heic", ".heif", ".avif", ".dng", ".raw", ".cr2", ".cr3",
    ".nef", ".nrw", ".arw", ".srf", ".sr2", ".orf", ".rw2", ".pef",
    ".raf", ".3fr", ".fff", ".iiq", ".k25", ".kdc", ".mrw", ".rwl",
    ".x3f", ".mp4", ".mov", ".m4v", ".avi", ".mkv", ".wmv", ".mpg",
    ".mpeg", ".3gp", ".mts", ".m2ts", ".webm", ".vob", ".ogv",
}
DEFAULT_ENV_FILE = Path(__file__).resolve().parent / ".env"
PICASA_EPOCH = datetime(1899, 12, 30)
PICASA_EDIT_KEYS = ("backuphash", "rotate", "crop", "filters", "redo")
REPORT_VERSION = 2


# --------------------------------------------------------------------------- #
# Settings and small helpers
# --------------------------------------------------------------------------- #

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
    default .env next to this script (used only if present).
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


def json_write(path: Path, data: Any) -> None:
    """Write JSON atomically (via a temporary file), with sorted keys."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def norm_path(value: str | Path) -> str:
    """Case-insensitive, platform-independent key for a path relative to source."""
    return str(value).replace("\\\\", "/").replace("\\", "/").casefold()


def album_display_name(token: str, properties: Mapping[str, Any]) -> str:
    """Album title from Picasa, or a fallback built from its token."""
    return str(properties.get("name") or f"Picasa album {token}").strip()


def picasa_date_to_iso(raw: str) -> str:
    """Convert Picasa's serial day count (days since 1899-12-30) to ISO, no timezone.

    Raises ValueError or OverflowError for invalid values.
    """
    return (PICASA_EPOCH + timedelta(days=float(raw))).isoformat(sep=" ", timespec="seconds")


# --------------------------------------------------------------------------- #
# Picasa metadata
# --------------------------------------------------------------------------- #

@dataclass
class IniContent:
    images: dict[str, dict[str, str]] = field(default_factory=dict)
    albums: dict[str, dict[str, str]] = field(default_factory=dict)
    folder: dict[str, str] = field(default_factory=dict)


def parse_ini(path: Path) -> IniContent:
    """Split one .picasa.ini into image sections, album definitions and folder metadata."""
    parser = configparser.ConfigParser(interpolation=None, strict=False, delimiters=("=",))
    parser.optionxform = str  # keep property names case-sensitive
    try:
        with path.open("r", encoding="utf-8-sig", errors="replace") as stream:
            parser.read_file(stream)
    except (OSError, configparser.Error) as exc:
        LOG.warning("Cannot parse %s: %s", path, exc)
        return IniContent()

    content = IniContent()
    for section in parser.sections():
        props = dict(parser.items(section, raw=True))
        lowered = section.casefold()
        if lowered == "picasa":
            # Describes the physical directory, not an image or album.
            content.folder.update(props)
        elif lowered.startswith(".album:"):
            token = section.split(":", 1)[1].strip()
            if token:
                content.albums.setdefault(token, {}).update(props)
        else:
            content.images.setdefault(section, {}).update(props)
    return content


@dataclass
class PicasaData:
    albums: dict[str, dict[str, Any]] = field(default_factory=dict)
    folders: dict[str, dict[str, Any]] = field(default_factory=dict)
    files: dict[str, dict[str, Any]] = field(default_factory=dict)
    ini_files: list[Path] = field(default_factory=list)
    warnings: list[dict[str, Any]] = field(default_factory=list)

    def album_names(self) -> dict[str, str]:
        return {token: album_display_name(token, album["properties"]) for token, album in self.albums.items()}


def scan_picasa(root: Path) -> PicasaData:
    """Read every .picasa.ini below root and resolve album memberships."""
    data = PicasaData()
    root = root.resolve()
    for ini in sorted(root.rglob(".picasa.ini")):
        data.ini_files.append(ini)
        content = parse_ini(ini)
        if content.folder:
            _add_folder(data, root, ini, content.folder)
        for token, props in content.albums.items():
            album = data.albums.setdefault(token, {"token": token, "properties": {}, "sourceIni": []})
            album["properties"].update(props)
            album["sourceIni"].append(str(ini))
        for filename, props in content.images.items():
            _add_image(data, root, ini, filename, props)
    _resolve_album_tokens(data)
    return data


def _add_folder(data: PicasaData, root: Path, ini: Path, metadata: dict[str, str]) -> None:
    relative_dir = ini.parent.relative_to(root).as_posix()
    folder = data.folders.setdefault(norm_path(relative_dir or "."), {
        "relativePath": relative_dir if relative_dir != "." else "",
        "properties": {},
        "sourceIni": [],
    })
    folder["properties"].update(metadata)
    source_ini = ini.relative_to(root).as_posix()
    if source_ini not in folder["sourceIni"]:
        folder["sourceIni"].append(source_ini)
    raw_date = metadata.get("date")
    if raw_date:
        try:
            folder["dateIso"] = picasa_date_to_iso(raw_date)
        except (ValueError, OverflowError):
            data.warnings.append({
                "type": "invalid_picasa_folder_date",
                "relativePath": folder["relativePath"],
                "value": raw_date,
                "sourceIni": source_ini,
            })


def _add_image(data: PicasaData, root: Path, ini: Path, filename: str, props: dict[str, str]) -> None:
    # Image-section names are relative to the directory containing the INI.
    # That directory is part of the key, so equal filenames stay distinct.
    local_path = (ini.parent / filename).resolve()
    try:
        relative_path = local_path.relative_to(root)
    except ValueError:
        data.warnings.append({
            "type": "image_path_outside_source",
            "fileName": filename,
            "sourceIni": str(ini),
            "resolvedPath": str(local_path),
        })
        return
    record = data.files.setdefault(norm_path(relative_path), {
        "fileName": filename,
        "relativePath": relative_path.as_posix(),
        "localPath": str(local_path),
        "properties": {},
        "sourceIni": [],
    })
    record["properties"].update(props)
    if str(ini) not in record["sourceIni"]:
        record["sourceIni"].append(str(ini))


def _resolve_album_tokens(data: PicasaData) -> None:
    """Split each file's 'albums' property; done after all INIs are read."""
    for record in data.files.values():
        tokens = [t.strip() for t in record["properties"].get("albums", "").split(",") if t.strip()]
        record["albumTokens"] = tokens
        for token in tokens:
            if token not in data.albums:
                data.warnings.append({
                    "type": "undefined_album_token",
                    "fileName": record["fileName"],
                    "relativePath": record["relativePath"],
                    "token": token,
                })


def build_local_index(root: Path, picasa: PicasaData) -> dict[str, Path]:
    """Map each Picasa file key to its existing, supported local file."""
    result: dict[str, Path] = {}
    root = root.resolve()
    for key, record in picasa.files.items():
        path = (root / record["relativePath"]).resolve()
        if not path.is_relative_to(root):
            LOG.warning("Ignoring image path outside source directory: %s", path)
        elif path.is_file() and path.suffix.casefold() in IMAGE_VIDEO_EXTENSIONS:
            result[key] = path
        else:
            LOG.warning("Local image file not found or unsupported: %s", path)
    return result


# --------------------------------------------------------------------------- #
# Scan-only mode
# --------------------------------------------------------------------------- #

def build_album_file_list(root: Path, picasa: PicasaData, local_paths: Mapping[str, Path]) -> dict[str, Any]:
    albums: list[dict[str, Any]] = []
    for token, definition in picasa.albums.items():
        files: set[str] = set()
        missing: set[str] = set()
        for key, record in picasa.files.items():
            if token in record["albumTokens"]:
                (files if key in local_paths else missing).add(record["relativePath"])
        albums.append({
            "token": token,
            "name": album_display_name(token, definition["properties"]),
            "files": sorted(files, key=str.casefold),
            "missingFiles": sorted(missing, key=str.casefold),
            "fileCount": len(files),
        })
    return {
        "createdAt": timestamp(),
        "source": str(root),
        "iniFileCount": len(picasa.ini_files),
        "albumCount": len(albums),
        "folderMetadataCount": len(picasa.folders),
        "imageSections": len(picasa.files),
        "folders": sorted(picasa.folders.values(), key=lambda item: item["relativePath"].casefold()),
        "albums": albums,
        "warnings": list(picasa.warnings),
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
    picasa = scan_picasa(root)
    result = build_album_file_list(root, picasa, build_local_index(root, picasa))
    json_write(output / "album_file_list.json", result)
    output.mkdir(parents=True, exist_ok=True)
    (output / "album_file_list.txt").write_text(album_file_list_text(result["albums"]), encoding="utf-8")
    LOG.info("Wrote %d albums to %s", result["albumCount"], output)
    LOG.info("Album file lists: %s and %s", output / "album_file_list.json", output / "album_file_list.txt")
    return result


# --------------------------------------------------------------------------- #
# Checksums
# --------------------------------------------------------------------------- #

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


# --------------------------------------------------------------------------- #
# Immich API client
# --------------------------------------------------------------------------- #

class ImmichClient(Protocol):
    """The part of the Immich API the migration needs (implemented by ImmichAPI)."""

    def server_version(self) -> str | None: ...
    def list_assets(self, page_size: int) -> list[dict[str, Any]]: ...
    def list_albums(self) -> list[dict[str, Any]]: ...
    def album_asset_ids(self, album_id: str, page_size: int) -> set[str]: ...
    def create_album(self, name: str, asset_ids: list[str]) -> Any: ...
    def add_album_assets(self, album_id: str, asset_ids: list[str]) -> Any: ...


class ImmichAPI:
    def __init__(self, url: str, api_key: str, timeout: int = 60, verify_tls: bool = True, debug: bool = False):
        try:
            import requests
        except ImportError as exc:
            raise RuntimeError("The Immich migration mode requires 'requests'. Install it with: pip install requests") from exc
        self.base = url.rstrip("/") + "/api"
        self.session = requests.Session()
        self.session.headers.update({"x-api-key": api_key, "Accept": "application/json"})
        self.session.verify = verify_tls
        self.timeout = timeout
        self.debug = debug

    def request(self, method: str, endpoint: str, **kwargs: Any) -> Any:
        url = self.base + "/" + endpoint.lstrip("/")
        if self.debug:
            # Never log the API key: only method, URL, query and JSON body.
            LOG.info("API -> %s %s params=%s json=%s", method, url,
                     kwargs.get("params"), json.dumps(kwargs.get("json"), ensure_ascii=False))
        response = self.session.request(method, url, timeout=self.timeout, **kwargs)
        if self.debug:
            LOG.info("API <- HTTP %s %s (%d bytes)", response.status_code, endpoint, len(response.content))
        if not response.ok:
            try:
                detail = response.json()
            except ValueError:
                detail = response.text[:2000]
            correlation = response.headers.get("X-Correlation-ID")
            raise RuntimeError(f"Immich API {method} {endpoint}: HTTP {response.status_code}: {detail}; "
                               f"X-Correlation-ID={correlation}")
        if response.status_code == 204 or not response.content:
            return None
        try:
            return response.json()
        except ValueError:
            return response.text

    def server_version(self) -> str | None:
        for endpoint in ("server/version", "server/about"):
            try:
                value = self.request("GET", endpoint)
            except Exception:
                continue
            if isinstance(value, dict):
                return str(value.get("version") or value.get("serverVersion") or value)
            return str(value)
        return None

    @staticmethod
    def _extract_items(payload: Any) -> tuple[list[dict[str, Any]], bool]:
        """Return (items, has_more) from current and legacy search response envelopes."""
        if not isinstance(payload, dict):
            raise RuntimeError(f"Unexpected search response type: {type(payload).__name__}")
        candidates = [payload]
        # A bare list keeps the pagination fields of the envelope around it.
        paging = {k: payload[k] for k in ("hasNextPage", "hasMore", "nextPage") if k in payload}
        for key in ("assets", "items", "data"):
            value = payload.get(key)
            if isinstance(value, dict):
                candidates.insert(0, value)
            elif isinstance(value, list):
                candidates.insert(0, {"items": value, **paging})
        for candidate in candidates:
            items = candidate.get("items")
            if isinstance(items, list):
                has_more = bool(candidate.get("hasNextPage", candidate.get("hasMore", False)))
                if candidate.get("nextPage") not in (None, "", False):
                    has_more = True
                return items, has_more
            if isinstance(candidate.get("assets"), list):
                return candidate["assets"], bool(candidate.get("hasNextPage", False))
        raise RuntimeError("Unrecognized /search/metadata response. Inspect the response and adapt pagination before applying.")

    @staticmethod
    def _next_cursor(payload: Any) -> str | None:
        if not isinstance(payload, dict):
            return None
        cursor = payload.get("nextCursor")
        # Some cursor-based responses nest the cursor in "assets".
        if not cursor and isinstance(payload.get("assets"), dict):
            cursor = payload["assets"].get("nextCursor")
        return str(cursor) if cursor else None

    def search_metadata(self, body: dict[str, Any]) -> Iterator[dict[str, Any]]:
        """Yield every result of POST /search/metadata, following page or cursor pagination.

        Never silently truncates: an empty page that claims more results is an error.
        """
        page = 1
        cursor: str | None = None
        while True:
            request_body = {**body, **({"cursor": cursor} if cursor else {"page": page})}
            payload = self.request("POST", "search/metadata", json=request_body)
            items, has_more = self._extract_items(payload)
            yield from (item for item in items if isinstance(item, dict))
            cursor = self._next_cursor(payload)
            if cursor:
                continue
            if not has_more:
                return
            if not items:
                raise RuntimeError(f"Search reports more pages but returned no items ({body}); "
                                   "refusing an incomplete result.")
            page += 1
            if page > 1_000_000:
                raise RuntimeError("Pagination safety limit reached.")

    def list_assets(self, page_size: int = 1000) -> list[dict[str, Any]]:
        # withExif is needed for exifInfo.fileSizeInByte, used by size matching.
        unique: dict[str, dict[str, Any]] = {}
        for item in self.search_metadata({"size": page_size, "withExif": True}):
            if item.get("id"):
                unique.setdefault(item["id"], item)
        return list(unique.values())

    def list_albums(self) -> list[dict[str, Any]]:
        result = self.request("GET", "albums", params={"isOwned": "true"})
        if isinstance(result, list):
            return result
        if isinstance(result, dict) and isinstance(result.get("items"), list):
            return result["items"]
        raise RuntimeError(f"Unexpected GET /albums response: {type(result).__name__}")

    def album_asset_ids(self, album_id: str, page_size: int = 1000) -> set[str]:
        body = {"size": page_size, "albumIds": [album_id]}
        return {str(item["id"]) for item in self.search_metadata(body) if item.get("id")}

    def create_album(self, name: str, asset_ids: list[str]) -> Any:
        return self.request("POST", "albums", json={"albumName": name, "assetIds": asset_ids})

    def add_album_assets(self, album_id: str, asset_ids: list[str]) -> Any:
        return self.request("PUT", f"albums/{album_id}/assets", json={"ids": asset_ids})


# --------------------------------------------------------------------------- #
# Matching local files to Immich assets
# --------------------------------------------------------------------------- #

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
        self.checksum_of: dict[int, str | None] = {}
        self._position: dict[int, int] = {}
        for position, asset in enumerate(self.assets):
            checksum = remote_checksum(asset)
            self._position[id(asset)] = position
            self.checksum_of[id(asset)] = checksum
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
        return self.checksum_of[id(asset)]


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


# --------------------------------------------------------------------------- #
# Migration mode
# --------------------------------------------------------------------------- #

@dataclass
class MatchResults:
    entries: list[dict[str, Any]] = field(default_factory=list)       # migration_report.json "matches"
    mapping: dict[str, Any] = field(default_factory=dict)             # asset_mapping.json
    archive: dict[str, Any] = field(default_factory=dict)             # metadata grouped by asset ID
    asset_ids_by_token: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    counts: Counter = field(default_factory=Counter)


def match_files(picasa: PicasaData, local_paths: Mapping[str, Path], index: AssetIndex,
                allow_name_only: bool) -> MatchResults:
    results = MatchResults()
    album_names = picasa.album_names()
    for key, record in picasa.files.items():
        base = {"fileName": record["fileName"], "relativePath": record["relativePath"]}
        local_path = local_paths.get(key)
        if not local_path:
            results.entries.append({**base, "status": "unmatched", "reason": "local_file_not_found"})
            results.counts["unmatched"] += 1
            continue
        match = match_local_file(local_path, index, allow_name_only)
        results.counts[match.status] += 1
        if not match.asset:
            results.entries.append({**base, "localPath": str(local_path), "status": match.status,
                                    "reason": match.reason,
                                    "candidateIds": [a.get("id") for a in match.candidates]})
            continue
        asset_id = str(match.asset["id"])
        results.entries.append({**base, "localPath": str(local_path), "immichAssetId": asset_id,
                                "status": match.status, "method": match.reason,
                                "albumTokens": record["albumTokens"]})
        results.mapping[key] = {**base, "localPath": str(local_path), "immichAssetId": asset_id,
                                "originalFileName": match.asset.get("originalFileName"),
                                "matchMethod": match.reason}
        for token in record["albumTokens"]:
            results.asset_ids_by_token[token].add(asset_id)
        _archive_match(results.archive, asset_id, match.asset, local_path, record, album_names)
    return results


def _archive_match(archive: dict[str, Any], asset_id: str, asset: Mapping[str, Any], local_path: Path,
                   record: Mapping[str, Any], album_names: Mapping[str, str]) -> None:
    """Merge a file's Picasa metadata into its asset record; one asset can have several local paths."""
    entry = archive.setdefault(asset_id, {
        "immichAssetId": asset_id,
        "originalFileName": asset.get("originalFileName"),
        "localPath": str(local_path),
        "localPaths": [],
        "picasa": {"allProperties": {}, "sourceIni": []},
        "picasaAlbums": [],
        "immich": {"editsApplied": [], "editsPending": ["rotate", "crop", "filters", "redo"]},
        "migration": {"status": "preserved", "version": 1},
    })
    if str(local_path) not in entry["localPaths"]:
        entry["localPaths"].append(str(local_path))
    picasa = entry["picasa"]
    picasa["allProperties"].update(record["properties"])
    picasa["sourceIni"] = sorted(set(picasa["sourceIni"]) | set(record["sourceIni"]))
    for key in PICASA_EDIT_KEYS:
        picasa[key] = record["properties"].get(key)
    for token in record["albumTokens"]:
        membership = {"token": token, "name": album_names.get(token, album_display_name(token, {}))}
        if membership not in entry["picasaAlbums"]:
            entry["picasaAlbums"].append(membership)


def sync_albums(api: ImmichClient, picasa: PicasaData, asset_ids_by_token: Mapping[str, set[str]],
                existing_albums: Iterable[Mapping[str, Any]], apply: bool, page_size: int,
                warnings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Plan (and with apply=True, perform) album creation and additions.

    Every Picasa album definition is reported, even without matched members.
    Different tokens with the same title are not merged, since they may be
    intentionally distinct albums. Existing Immich albums are found by
    case-insensitive name; an ambiguous name is reported and left alone.
    """
    by_name: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for album in existing_albums:
        by_name[str(album.get("albumName", "")).casefold()].append(album)

    reports = []
    for token, album in picasa.albums.items():
        name = album_display_name(token, album["properties"])
        desired = sorted(asset_ids_by_token.get(token, set()))
        existing = by_name.get(name.casefold(), [])
        report = {"token": token, "name": name, "picasaProperties": album["properties"],
                  "matchedAssetCount": len(desired), "action": "existing" if existing else "create",
                  "addedAssetIds": [], "alreadyPresentAssetCount": 0}
        if len(existing) > 1:
            warnings.append({"type": "duplicate_immich_album_names", "albumName": name,
                             "albumIds": [x.get("id") for x in existing]})
            report["action"] = "ambiguous_existing_album"
        elif existing:
            _update_existing_album(api, str(existing[0]["id"]), desired, report, apply, page_size, warnings)
        else:
            report["addedAssetIds"] = desired
            if apply:
                created = api.create_album(name, desired)
                if not isinstance(created, dict) or not created.get("id"):
                    raise RuntimeError(f"Album creation response did not contain an id for {name!r}: {created!r}")
                report["immichAlbumId"] = str(created["id"])
                report["action"] = "created"
        reports.append(report)
    return reports


def _update_existing_album(api: ImmichClient, album_id: str, desired: list[str], report: dict[str, Any],
                           apply: bool, page_size: int, warnings: list[dict[str, Any]]) -> None:
    try:
        present = api.album_asset_ids(album_id, page_size)
    except Exception as exc:
        warnings.append({"type": "album_membership_read_failed", "albumName": report["name"], "error": str(exc)})
        report["action"] = "skipped_membership_read_failed"
        return
    missing = [asset_id for asset_id in desired if asset_id not in present]
    report["alreadyPresentAssetCount"] = len(desired) - len(missing)
    report["addedAssetIds"] = missing
    report["action"] = "add_missing_members" if missing else "already_complete"
    if apply and missing:
        api.add_album_assets(album_id, missing)
        report["action"] = "updated_existing_album"


def copy_original_ini(root: Path, output: Path, ini_files: Iterable[Path]) -> None:
    target_root = output / "original_ini"
    for source in ini_files:
        target = target_root / source.relative_to(root)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def run_migration(api: ImmichClient, root: Path, output: Path, *, apply: bool = False,
                  allow_name_only: bool = False, page_size: int = 500) -> dict[str, Any]:
    """Match Picasa files to Immich assets, sync albums and write all reports."""
    LOG.info("Scanning Picasa metadata under %s", root)
    picasa = scan_picasa(root)
    copy_original_ini(root, output, picasa.ini_files)
    LOG.info("Found %d .picasa.ini files, %d folder metadata records, %d album definitions, %d image/video sections",
             len(picasa.ini_files), len(picasa.folders), len(picasa.albums), len(picasa.files))

    version = api.server_version()
    LOG.info("Immich server: %s", version or "version endpoint unavailable")
    LOG.info("Retrieving existing Immich assets (this may take a while)")
    index = AssetIndex(api.list_assets(page_size))
    LOG.info("Retrieved %d Immich assets", len(index))
    existing_albums = api.list_albums()

    local_paths = build_local_index(root, picasa)
    matches = match_files(picasa, local_paths, index, allow_name_only)
    warnings = list(picasa.warnings)
    albums = sync_albums(api, picasa, matches.asset_ids_by_token, existing_albums, apply, page_size, warnings)

    counts = matches.counts
    report = {
        "scriptVersion": REPORT_VERSION,
        "createdAt": timestamp(),
        "mode": "apply" if apply else "dry-run",
        "immichServerVersion": version,
        "matches": matches.entries,
        "albums": albums,
        "warnings": warnings,
        "summary": {
            "iniFiles": len(picasa.ini_files), "picasaAlbumDefinitions": len(picasa.albums),
            "picasaImageSections": len(picasa.files),
            "distinctLocalImagePaths": len(local_paths), "immichAssetsScanned": len(index),
            "matchedAssets": counts["matched"] + counts["matched_low_confidence"],
            "highConfidenceMatches": counts["matched"], "lowConfidenceMatches": counts["matched_low_confidence"],
            "unmatched": counts["unmatched"], "ambiguous": counts["ambiguous"],
            "albumsProcessed": len(albums),
        },
    }
    json_write(output / "asset_mapping.json", matches.mapping)
    json_write(output / "picasa_migration_metadata.json",
               {"assets": matches.archive, "albums": picasa.albums, "folders": picasa.folders})
    json_write(output / "migration_report.json", report)
    return report


# --------------------------------------------------------------------------- #
# Command line
# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("source", type=Path, help="Root folder containing Picasa folders and .picasa.ini files")
    parser.add_argument("--output", type=Path, default=Path("picasa_migration"), help="Output/archive directory")
    parser.add_argument("--scan-only", action="store_true",
                        help="Only scan Picasa folders and write album file lists; no Immich connection or API key required")
    parser.add_argument("--url", help="Immich base URL, e.g. http://192.168.1.100:2283 (or IMMICH_URL in the environment or .env)")
    parser.add_argument("--api-key", help="API key (or IMMICH_API_KEY in the environment or .env)")
    parser.add_argument("--env-file", type=Path,
                        help=f"File with IMMICH_URL / IMMICH_API_KEY lines (default: {DEFAULT_ENV_FILE.name} next to this script, if present)")
    parser.add_argument("--apply", action="store_true", help="Actually create albums and add assets; default is dry-run")
    parser.add_argument("--allow-name-only", action="store_true", help="Allow unique filename-only matches (lower confidence)")
    parser.add_argument("--no-verify-tls", action="store_true", help="Disable TLS certificate verification")
    parser.add_argument("--page-size", type=int, default=500, help="Immich search page size (default: 500)")
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    parser.add_argument("--debug-api", action="store_true",
                        help="Log every Immich API request (method, URL, body) and response status")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(level=getattr(logging, args.log_level), format="%(levelname)s: %(message)s")
    root = args.source.expanduser().resolve()
    output = args.output.expanduser().resolve()
    if not root.is_dir():
        parser.error(f"Source directory does not exist: {root}")

    if args.scan_only:
        if args.apply:
            parser.error("--apply cannot be combined with --scan-only")
        LOG.info("Scanning Picasa folders (scan-only mode; Immich will not be contacted)")
        result = run_scan_only(root, output)
        LOG.info("Found %d .picasa.ini files, %d folder metadata records, %d album definitions, %d image/video sections",
                 result["iniFileCount"], result["folderMetadataCount"], result["albumCount"], result["imageSections"])
        return 0

    try:
        credentials = resolve_credentials(args.url, args.api_key, args.env_file)
    except ValueError as exc:
        parser.error(str(exc))
    if credentials.env_file_used:
        LOG.info("Read settings from %s", credentials.env_file_used)
    if not credentials.url or not credentials.api_key:
        parser.error("Set --url and --api-key, or IMMICH_URL and IMMICH_API_KEY as environment variables "
                     "or in a .env file (not needed with --scan-only)")
    if args.page_size < 1:
        parser.error("--page-size must be positive")

    api = ImmichAPI(credentials.url, credentials.api_key, args.timeout, not args.no_verify_tls, debug=args.debug_api)
    report = run_migration(api, root, output, apply=args.apply, allow_name_only=args.allow_name_only,
                           page_size=args.page_size)
    LOG.info("Finished in %s mode", "APPLY" if args.apply else "DRY-RUN")
    LOG.info("Summary: %s", json.dumps(report["summary"], ensure_ascii=False))
    LOG.info("Reports written to %s", output)
    if not args.apply:
        LOG.info("No Immich changes were made. Review migration_report.json, then rerun with --apply.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        LOG.error("Interrupted")
        raise SystemExit(130)
    except Exception as exc:
        LOG.exception("Migration stopped: %s", exc)
        raise SystemExit(1)
