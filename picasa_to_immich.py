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
import sys
import time
from datetime import datetime, timedelta
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable


LOG = logging.getLogger("picasa_to_immich")
IMAGE_VIDEO_EXTENSIONS = {
    ".jpg", ".jpeg", ".jpe", ".png", ".gif", ".bmp", ".tif", ".tiff",
    ".webp", ".heic", ".heif", ".avif", ".dng", ".raw", ".cr2", ".cr3",
    ".nef", ".nrw", ".arw", ".srf", ".sr2", ".orf", ".rw2", ".pef",
    ".raf", ".3fr", ".fff", ".iiq", ".k25", ".kdc", ".mrw", ".rwl",
    ".x3f", ".mp4", ".mov", ".m4v", ".avi", ".mkv", ".wmv", ".mpg",
    ".mpeg", ".3gp", ".mts", ".m2ts", ".webm", ".vob", ".ogv",
}


def json_write(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def norm_name(value: str) -> str:
    return value.casefold()


def norm_path(value: str | Path) -> str:
    """Case-insensitive, platform-independent key for a path relative to source."""
    return str(value).replace("\\\\", "/").replace("\\", "/").casefold()


def parse_ini(path: Path) -> tuple[dict[str, dict[str, str]], dict[str, dict[str, str]], dict[str, str]]:
    """Return (image sections, album definitions, folder metadata)."""
    parser = configparser.ConfigParser(interpolation=None, strict=False, delimiters=("=",))
    parser.optionxform = str
    try:
        with path.open("r", encoding="utf-8-sig", errors="replace") as stream:
            parser.read_file(stream)
    except (OSError, configparser.Error) as exc:
        LOG.warning("Cannot parse %s: %s", path, exc)
        return {}, {}, {}

    images: dict[str, dict[str, str]] = {}
    albums: dict[str, dict[str, str]] = {}
    folder_metadata: dict[str, str] = {}
    for section in parser.sections():
        props = dict(parser.items(section, raw=True))
        if section.casefold() == "picasa":
            # This describes the physical directory, not an image or album.
            folder_metadata.update(props)
        elif section.casefold().startswith(".album:"):
            token = section.split(":", 1)[1].strip()
            if token:
                # Keep every property, not only name/date/token.
                albums[token] = {**albums.get(token, {}), **props, "_sourceIni": str(path)}
        else:
            images[section] = {**images.get(section, {}), **props, "_sourceIni": str(path)}
    return images, albums, folder_metadata


@dataclass
class PicasaData:
    albums: dict[str, dict[str, Any]] = field(default_factory=dict)
    folders: dict[str, dict[str, Any]] = field(default_factory=dict)
    files: dict[str, dict[str, Any]] = field(default_factory=dict)
    ini_files: list[Path] = field(default_factory=list)
    warnings: list[dict[str, Any]] = field(default_factory=list)


def scan_picasa(root: Path) -> PicasaData:
    data = PicasaData()
    root = root.resolve()

    for ini in sorted(root.rglob(".picasa.ini")):
        data.ini_files.append(ini)
        images, albums, folder_metadata = parse_ini(ini)

        if folder_metadata:
            relative_dir = ini.parent.relative_to(root).as_posix()
            folder_key = norm_path(relative_dir or ".")
            folder = data.folders.setdefault(folder_key, {
                "relativePath": relative_dir if relative_dir != "." else "",
                "properties": {},
                "sourceIni": [],
            })
            folder["properties"].update(folder_metadata)
            source_ini = str(ini.relative_to(root).as_posix())
            if source_ini not in folder["sourceIni"]:
                folder["sourceIni"].append(source_ini)
            # Picasa stores this as a serial day count from 1899-12-30.
            # Keep the original value and add a timezone-unspecified ISO value.
            raw_date = folder_metadata.get("date")
            if raw_date:
                try:
                    serial = float(raw_date)
                    folder["dateIso"] = (datetime(1899, 12, 30) + timedelta(days=serial)).isoformat(sep=" ", timespec="seconds")
                except (ValueError, OverflowError):
                    data.warnings.append({
                        "type": "invalid_picasa_folder_date",
                        "relativePath": folder["relativePath"],
                        "value": raw_date,
                        "sourceIni": source_ini,
                    })

        for token, props in albums.items():
            current = data.albums.setdefault(token, {"token": token, "properties": {}, "sourceIni": []})
            current["properties"].update({k: v for k, v in props.items() if k != "_sourceIni"})
            current["sourceIni"].append(str(ini))

        # Image-section names are relative to the directory containing this INI.
        # Include that directory in the key so equal filenames remain distinct.
        for filename, props in images.items():
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
                continue

            key = norm_path(relative_path)
            current = data.files.setdefault(key, {
                "fileName": filename,
                "relativePath": relative_path.as_posix(),
                "localPath": str(local_path),
                "properties": {},
                "sourceIni": [],
            })
            current["properties"].update({k: v for k, v in props.items() if k != "_sourceIni"})
            if str(ini) not in current["sourceIni"]:
                current["sourceIni"].append(str(ini))

    # Resolve memberships only after every INI has been read.
    for item in data.files.values():
        raw = item["properties"].get("albums", "")
        tokens = [x.strip() for x in raw.split(",") if x.strip()]
        item["albumTokens"] = tokens
        for token in tokens:
            if token not in data.albums:
                data.warnings.append({
                    "type": "undefined_album_token",
                    "fileName": item["fileName"],
                    "relativePath": item["relativePath"],
                    "token": token,
                })
    return data


def sha1_file(path: Path, chunk_size: int = 4 * 1024 * 1024) -> tuple[str, int]:
    digest = hashlib.sha1()
    size = 0
    with path.open("rb") as stream:
        while chunk := stream.read(chunk_size):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def checksum_forms(hex_digest: str) -> set[str]:
    raw = bytes.fromhex(hex_digest)
    return {hex_digest.lower(), base64.b64encode(raw).decode("ascii")}


class ImmichAPI:
    def __init__(self, url: str, api_key: str, timeout: int = 60, verify_tls: bool = True):
        try:
            import requests
        except ImportError as exc:
            raise RuntimeError("The Immich migration mode requires 'requests'. Install it with: pip install requests") from exc
        self.base = url.rstrip("/") + "/api"
        self.session = requests.Session()
        self.session.headers.update({"x-api-key": api_key, "Accept": "application/json"})
        self.timeout = timeout
        self.session.verify = verify_tls

    def request(self, method: str, endpoint: str, **kwargs: Any) -> Any:
        url = self.base + "/" + endpoint.lstrip("/")
        response = self.session.request(method, url, timeout=self.timeout, **kwargs)
        if not response.ok:
            try:
                detail = response.json()
            except ValueError:
                detail = response.text[:2000]
            correlation = response.headers.get("X-Correlation-ID")
            raise RuntimeError(f"Immich API {method} {endpoint}: HTTP {response.status_code}: {detail}; X-Correlation-ID={correlation}")
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
                if isinstance(value, dict):
                    return str(value.get("version") or value.get("serverVersion") or value)
                return str(value)
            except Exception:
                continue
        return None

    @staticmethod
    def _extract_items(payload: Any) -> tuple[list[dict[str, Any]], bool]:
        """Accept current and legacy Immich search response envelopes."""
        if not isinstance(payload, dict):
            raise RuntimeError(f"Unexpected search response type: {type(payload).__name__}")
        candidates = [payload]
        for key in ("assets", "items", "data"):
            value = payload.get(key)
            if isinstance(value, dict):
                candidates.insert(0, value)
            elif isinstance(value, list):
                candidates.insert(0, {"items": value})
        for candidate in candidates:
            items = candidate.get("items")
            if isinstance(items, list):
                has_more = bool(candidate.get("hasNextPage", candidate.get("hasMore", False)))
                next_page = candidate.get("nextPage")
                if next_page not in (None, "", False):
                    has_more = True
                return items, has_more
            if isinstance(candidate.get("assets"), list):
                return candidate["assets"], bool(candidate.get("hasNextPage", False))
        raise RuntimeError("Unrecognized /search/metadata response. Inspect the response and adapt pagination before applying.")

    def list_assets(self, page_size: int = 1000) -> list[dict[str, Any]]:
        """Enumerate assets. Legacy page/size shape is retained for compatibility.

        Immich v3.2 also has a cursor-based search shape. This function accepts
        cursor responses if returned by the server; it never silently truncates.
        """
        all_items: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        page = 1
        cursor: str | None = None
        while True:
            body: dict[str, Any] = {"size": page_size}
            if cursor:
                body["cursor"] = cursor
            else:
                body["page"] = page
            payload = self.request("POST", "search/metadata", json=body)
            items, has_more = self._extract_items(payload)
            for item in items:
                if isinstance(item, dict) and item.get("id") and item["id"] not in seen_ids:
                    seen_ids.add(item["id"])
                    all_items.append(item)
            next_cursor = payload.get("nextCursor") if isinstance(payload, dict) else None
            # Some cursor-based responses nest the cursor in assets.
            if not next_cursor and isinstance(payload, dict) and isinstance(payload.get("assets"), dict):
                next_cursor = payload["assets"].get("nextCursor")
            if next_cursor:
                cursor = str(next_cursor)
                continue
            if not has_more:
                break
            page += 1
            # A page-based server must return another non-empty page; otherwise
            # stop with an error rather than claim a complete inventory.
            if not items:
                raise RuntimeError("Search reports more pages but returned no items; refusing incomplete inventory.")
            if page > 1_000_000:
                raise RuntimeError("Pagination safety limit reached.")
        return all_items

    def list_albums(self) -> list[dict[str, Any]]:
        result = self.request("GET", "albums", params={"isOwned": "true"})
        if isinstance(result, list):
            return result
        if isinstance(result, dict) and isinstance(result.get("items"), list):
            return result["items"]
        raise RuntimeError(f"Unexpected GET /albums response: {type(result).__name__}")

    def album_asset_ids(self, album_id: str, page_size: int = 1000) -> set[str]:
        ids: set[str] = set()
        page = 1
        while True:
            body = {"page": page, "size": page_size, "albumIds": [album_id]}
            payload = self.request("POST", "search/metadata", json=body)
            items, has_more = self._extract_items(payload)
            ids.update(str(x["id"]) for x in items if isinstance(x, dict) and x.get("id"))
            if not has_more:
                return ids
            if not items:
                raise RuntimeError(f"Album asset search for {album_id} returned an empty page while indicating more results")
            page += 1

    def create_album(self, name: str, asset_ids: list[str]) -> dict[str, Any]:
        return self.request("POST", "albums", json={"albumName": name, "assetIds": asset_ids})

    def add_album_assets(self, album_id: str, asset_ids: list[str]) -> Any:
        return self.request("PUT", f"albums/{album_id}/assets", json={"ids": asset_ids})


@dataclass
class Match:
    status: str
    asset: dict[str, Any] | None = None
    candidates: list[dict[str, Any]] = field(default_factory=list)
    reason: str = ""


def remote_size(asset: dict[str, Any]) -> int | None:
    for key in ("fileSize", "originalFileSize", "size"):
        value = asset.get(key)
        if isinstance(value, (int, float)):
            return int(value)
    exif = asset.get("exifInfo")
    if isinstance(exif, dict):
        for key in ("fileSize", "fileSizeInBytes"):
            value = exif.get(key)
            if isinstance(value, (int, float)):
                return int(value)
    return None


def remote_checksum(asset: dict[str, Any]) -> str | None:
    value = asset.get("checksum")
    if isinstance(value, str) and value:
        return value.strip()
    return None


def match_asset(local_path: Path, assets: list[dict[str, Any]], allow_name_only: bool) -> Match:
    try:
        local_sha1, local_size = sha1_file(local_path)
    except OSError as exc:
        return Match("unmatched", reason=f"cannot_read_local_file: {exc}")
    local_checksums = checksum_forms(local_sha1)
    filename = local_path.name.casefold()

    same_name = [a for a in assets if str(a.get("originalFileName", "")).casefold() == filename]
    # SHA-1 is authoritative when the server provides a checksum.
    sha_matches = [a for a in assets if remote_checksum(a) in local_checksums]
    if len(sha_matches) == 1:
        return Match("matched", sha_matches[0], reason="sha1")
    if len(sha_matches) > 1:
        return Match("ambiguous", candidates=sha_matches, reason="multiple_sha1_matches")

    # A same-name asset with a different known checksum is explicitly rejected,
    # even when its file size happens to match.
    checksum_mismatch = [a for a in same_name if remote_checksum(a) is not None and remote_checksum(a) not in local_checksums]
    if checksum_mismatch:
        return Match("unmatched", candidates=checksum_mismatch, reason="server_checksum_mismatch")

    size_matches = [a for a in same_name if remote_size(a) == local_size]
    if len(size_matches) == 1:
        return Match("matched", size_matches[0], reason="filename_and_size_no_server_checksum")
    if len(size_matches) > 1:
        return Match("ambiguous", candidates=size_matches, reason="multiple_filename_size_matches")

    if allow_name_only and same_name:
        if len(same_name) == 1:
            return Match("matched_low_confidence", same_name[0], reason="filename_only_explicitly_allowed")
        return Match("ambiguous", candidates=same_name, reason="multiple_filename_matches")
    if same_name:
        return Match("unmatched", candidates=same_name, reason="filename_found_but_size_unavailable_or_different")
    return Match("unmatched", reason="no_candidate")


def build_local_index(root: Path, picasa: PicasaData) -> dict[str, Path]:
    """Resolve each Picasa image section to its own path under the source root."""
    result: dict[str, Path] = {}
    root = root.resolve()

    for key, record in picasa.files.items():
        path = (root / record["relativePath"]).resolve()
        try:
            path.relative_to(root)
        except ValueError:
            LOG.warning("Ignoring image path outside source directory: %s", path)
            continue

        if path.is_file() and path.suffix.casefold() in IMAGE_VIDEO_EXTENSIONS:
            result[key] = path
        else:
            LOG.warning("Local image file not found or unsupported: %s", path)

    return result


def write_album_file_list(root: Path, output: Path, picasa: PicasaData) -> dict[str, Any]:
    """Write Picasa album membership as source-relative file paths, without Immich."""
    local_paths = build_local_index(root, picasa)
    albums: list[dict[str, Any]] = []
    warnings = list(picasa.warnings)

    for token, definition in picasa.albums.items():
        name = str(definition["properties"].get("name") or f"Picasa album {token}").strip()
        files: list[str] = []
        missing_files: list[str] = []
        for key, pfile in picasa.files.items():
            if token not in pfile.get("albumTokens", []):
                continue
            relative_path = pfile["relativePath"]
            if key in local_paths:
                files.append(relative_path)
            else:
                missing_files.append(relative_path)
        albums.append({
            "token": token,
            "name": name,
            "files": sorted(set(files), key=str.casefold),
            "missingFiles": sorted(set(missing_files), key=str.casefold),
            "fileCount": len(set(files)),
        })

    result = {
        "createdAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "source": str(root),
        "iniFileCount": len(picasa.ini_files),
        "albumCount": len(albums),
        "folderMetadataCount": len(picasa.folders),
        "imageSections": len(picasa.files),
        "folders": sorted(picasa.folders.values(), key=lambda item: item["relativePath"].casefold()),
        "albums": albums,
        "warnings": warnings,
    }
    json_write(output / "album_file_list.json", result)

    # Convenient plain-text version: one album heading followed by relative paths.
    lines: list[str] = []
    for album in albums:
        lines.append(f"[{album['name']}] (token: {album['token']})")
        lines.extend(album["files"])
        if album["missingFiles"]:
            lines.append("# Missing or unsupported files:")
            lines.extend(f"# {name}" for name in album["missingFiles"])
        lines.append("")
    output.mkdir(parents=True, exist_ok=True)
    (output / "album_file_list.txt").write_text("\n".join(lines), encoding="utf-8")

    LOG.info("Wrote %d albums to %s", len(albums), output)
    LOG.info("Album file lists: %s and %s", output / "album_file_list.json", output / "album_file_list.txt")
    return result


def copy_original_ini(root: Path, output: Path, ini_files: Iterable[Path]) -> None:
    target_root = output / "original_ini"
    for source in ini_files:
        relative = source.relative_to(root)
        target = target_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("source", type=Path, help="Root folder containing Picasa folders and .picasa.ini files")
    parser.add_argument("--output", type=Path, default=Path("picasa_migration"), help="Output/archive directory")
    parser.add_argument("--scan-only", action="store_true",
                        help="Only scan Picasa folders and write album file lists; no Immich connection or API key required")
    parser.add_argument("--url", default=os.environ.get("IMMICH_URL"), help="Immich base URL, e.g. http://192.168.1.100:2283")
    parser.add_argument("--api-key", default=os.environ.get("IMMICH_API_KEY"), help="API key (or IMMICH_API_KEY environment variable)")
    parser.add_argument("--apply", action="store_true", help="Actually create albums and add assets; default is dry-run")
    parser.add_argument("--allow-name-only", action="store_true", help="Allow unique filename-only matches (lower confidence)")
    parser.add_argument("--no-verify-tls", action="store_true", help="Disable TLS certificate verification")
    parser.add_argument("--page-size", type=int, default=500, help="Immich search page size (default: 500)")
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = parser.parse_args()

    logging.basicConfig(level=getattr(logging, args.log_level), format="%(levelname)s: %(message)s")
    root = args.source.expanduser().resolve()
    output = args.output.expanduser().resolve()
    if not root.is_dir():
        parser.error(f"Source directory does not exist: {root}")
    if args.scan_only:
        if args.apply:
            parser.error("--apply cannot be combined with --scan-only")
        LOG.info("Scanning Picasa folders (scan-only mode; Immich will not be contacted)")
        picasa = scan_picasa(root)
        result = write_album_file_list(root, output, picasa)
        LOG.info("Found %d .picasa.ini files, %d folder metadata records, %d album definitions, %d image/video sections",
                 result["iniFileCount"], result["folderMetadataCount"], result["albumCount"], result["imageSections"])
        return 0

    if not args.url or not args.api_key:
        parser.error("Set --url and --api-key, or IMMICH_URL and IMMICH_API_KEY environment variables (not needed with --scan-only)")
    if args.page_size < 1:
        parser.error("--page-size must be positive")

    LOG.info("Scanning Picasa metadata under %s", root)
    picasa = scan_picasa(root)
    copy_original_ini(root, output, picasa.ini_files)
    LOG.info("Found %d .picasa.ini files, %d folder metadata records, %d album definitions, %d image/video sections", len(picasa.ini_files), len(picasa.folders), len(picasa.albums), len(picasa.files))

    api = ImmichAPI(args.url, args.api_key, args.timeout, not args.no_verify_tls)
    version = api.server_version()
    LOG.info("Immich server: %s", version or "version endpoint unavailable")
    LOG.info("Retrieving existing Immich assets (this may take a while)")
    assets = api.list_assets(args.page_size)
    LOG.info("Retrieved %d Immich assets", len(assets))
    existing_albums = api.list_albums()
    album_by_name: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for album in existing_albums:
        album_by_name[norm_name(str(album.get("albumName", "")))].append(album)

    local_paths = build_local_index(root, picasa)
    report: dict[str, Any] = {
        "scriptVersion": 2,
        "createdAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "mode": "apply" if args.apply else "dry-run",
        "immichServerVersion": version,
        "summary": {}, "matches": [], "albums": [], "warnings": picasa.warnings,
    }
    mapping: dict[str, Any] = {}
    archive: dict[str, Any] = {}
    matched_by_token: dict[str, list[str]] = defaultdict(list)
    match_counts = defaultdict(int)

    for key, pfile in picasa.files.items():
        local_path = local_paths.get(key)
        if not local_path:
            entry = {"fileName": pfile["fileName"], "relativePath": pfile["relativePath"],
                     "status": "unmatched", "reason": "local_file_not_found"}
            report["matches"].append(entry)
            match_counts["unmatched"] += 1
            continue
        match = match_asset(local_path, assets, args.allow_name_only)
        if not match.asset:
            entry = {"fileName": pfile["fileName"], "relativePath": pfile["relativePath"],
                     "localPath": str(local_path), "status": match.status, "reason": match.reason,
                     "candidateIds": [a.get("id") for a in match.candidates]}
            report["matches"].append(entry)
            match_counts[match.status] += 1
            continue
        asset = match.asset
        asset_id = str(asset["id"])
        match_counts[match.status] += 1
        mapping[key] = {"fileName": pfile["fileName"], "relativePath": pfile["relativePath"],
                        "localPath": str(local_path), "immichAssetId": asset_id,
                        "originalFileName": asset.get("originalFileName"), "matchMethod": match.reason}
        for token in pfile["albumTokens"]:
            matched_by_token[token].append(asset_id)
        # Merge by Immich UUID: one asset can be referenced in multiple INIs/albums.
        record = archive.setdefault(asset_id, {
            "immichAssetId": asset_id,
            "originalFileName": asset.get("originalFileName"),
            "localPath": str(local_path),
            "localPaths": [],
            "picasa": {"allProperties": {}, "sourceIni": []},
            "immich": {"editsApplied": [], "editsPending": ["rotate", "crop", "filters", "redo"]},
            "migration": {"status": "preserved", "version": 1},
        })
        if str(local_path) not in record["localPaths"]:
            record["localPaths"].append(str(local_path))
        record["picasa"]["allProperties"].update(pfile["properties"])
        record["picasa"]["sourceIni"] = sorted(set(record["picasa"]["sourceIni"] + pfile["sourceIni"]))
        for edit_key in ("backuphash", "rotate", "crop", "filters", "redo"):
            record["picasa"][edit_key] = pfile["properties"].get(edit_key)
        report["matches"].append({"fileName": pfile["fileName"], "relativePath": pfile["relativePath"],
                                  "localPath": str(local_path), "immichAssetId": asset_id,
                                  "status": match.status, "method": match.reason,
                                  "albumTokens": pfile["albumTokens"]})

    # Preserve all album definitions, even if no member could be matched.
    # Two different Picasa tokens with the same title are not automatically
    # merged: doing so could combine two intentionally distinct albums.
    album_name_to_id: dict[str, str] = {}
    picasa_name_tokens: dict[str, list[str]] = defaultdict(list)
    for token, definition in picasa.albums.items():
        title = str(definition["properties"].get("name") or f"Picasa album {token}").strip()
        picasa_name_tokens[norm_name(title)].append(token)
    for token, album in picasa.albums.items():
        props = album["properties"]
        name = str(props.get("name") or f"Picasa album {token}").strip()
        desired_ids = sorted(set(matched_by_token.get(token, [])))
        existing = album_by_name.get(norm_name(name), [])
        action = "existing" if existing else "create"
        if len(existing) > 1:
            report["warnings"].append({"type": "duplicate_immich_album_names", "albumName": name,
                                       "albumIds": [x.get("id") for x in existing]})
            action = "ambiguous_existing_album"
        album_report = {"token": token, "name": name, "picasaProperties": props,
                        "matchedAssetCount": len(desired_ids), "action": action, "addedAssetIds": [], "alreadyPresentAssetCount": 0}
        if len(existing) == 1:
            album_id = str(existing[0]["id"])
            album_name_to_id[token] = album_id
            try:
                present = api.album_asset_ids(album_id, args.page_size)
            except Exception as exc:
                report["warnings"].append({"type": "album_membership_read_failed", "albumName": name, "error": str(exc)})
                album_report["action"] = "skipped_membership_read_failed"
                report["albums"].append(album_report)
                continue
            missing = [asset_id for asset_id in desired_ids if asset_id not in present]
            album_report["alreadyPresentAssetCount"] = len(desired_ids) - len(missing)
            album_report["addedAssetIds"] = missing
            album_report["action"] = "add_missing_members" if missing else "already_complete"
            if args.apply and missing:
                api.add_album_assets(album_id, missing)
                album_report["action"] = "updated_existing_album"
        elif not existing:
            album_report["addedAssetIds"] = desired_ids
            if args.apply:
                created = api.create_album(name, desired_ids)
                if not isinstance(created, dict) or not created.get("id"):
                    raise RuntimeError(f"Album creation response did not contain an id for {name!r}: {created!r}")
                album_name_to_id[token] = str(created["id"])
                album_report["immichAlbumId"] = str(created["id"])
                album_report["action"] = "created"
        report["albums"].append(album_report)

    # Attach album membership information to every asset archive record.
    token_to_album_name = {token: str(info["properties"].get("name") or f"Picasa album {token}") for token, info in picasa.albums.items()}
    for key, pfile in picasa.files.items():
        local = local_paths.get(key)
        if not local:
            continue
        # Resolve by original mapping record; avoid rebuilding matching decisions.
        mapped = mapping.get(key)
        if not mapped:
            continue
        record = archive[mapped["immichAssetId"]]
        memberships = record.setdefault("picasaAlbums", [])
        for token in pfile["albumTokens"]:
            item = {"token": token, "name": token_to_album_name.get(token, f"Picasa album {token}")}
            if item not in memberships:
                memberships.append(item)

    report["summary"] = {
        "iniFiles": len(picasa.ini_files), "picasaAlbumDefinitions": len(picasa.albums),
        "picasaImageSections": len(picasa.files),
        "distinctLocalImagePaths": len(local_paths), "immichAssetsScanned": len(assets),
        "matchedAssets": match_counts["matched"] + match_counts["matched_low_confidence"],
        "highConfidenceMatches": match_counts["matched"], "lowConfidenceMatches": match_counts["matched_low_confidence"],
        "unmatched": match_counts["unmatched"], "ambiguous": match_counts["ambiguous"],
        "albumsProcessed": len(report["albums"]),
    }
    json_write(output / "asset_mapping.json", mapping)
    json_write(output / "picasa_migration_metadata.json", {"assets": archive, "albums": picasa.albums, "folders": picasa.folders})
    json_write(output / "migration_report.json", report)

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
