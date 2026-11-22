"""Reading Picasa metadata (.picasa.ini files). Has no Immich dependency."""

from __future__ import annotations

import configparser
import logging
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Mapping

from .util import norm_path

LOG = logging.getLogger(__name__)

IMAGE_VIDEO_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".jpe",
    ".png",
    ".gif",
    ".bmp",
    ".tif",
    ".tiff",
    ".webp",
    ".heic",
    ".heif",
    ".avif",
    ".dng",
    ".raw",
    ".cr2",
    ".cr3",
    ".nef",
    ".nrw",
    ".arw",
    ".srf",
    ".sr2",
    ".orf",
    ".rw2",
    ".pef",
    ".raf",
    ".3fr",
    ".fff",
    ".iiq",
    ".k25",
    ".kdc",
    ".mrw",
    ".rwl",
    ".x3f",
    ".mp4",
    ".mov",
    ".m4v",
    ".avi",
    ".mkv",
    ".wmv",
    ".mpg",
    ".mpeg",
    ".3gp",
    ".mts",
    ".m2ts",
    ".webm",
    ".vob",
    ".ogv",
}
PICASA_EPOCH = datetime(1899, 12, 30)
INI_NAME = ".picasa.ini"


def album_display_name(token: str, properties: Mapping[str, Any]) -> str:
    """Album title from Picasa, or a fallback built from its token."""
    return str(properties.get("name") or f"Picasa album {token}").strip()


def picasa_date_to_iso(raw: str) -> str:
    """Convert Picasa's serial day count (days since 1899-12-30) to ISO, no timezone.

    Raises ValueError or OverflowError for invalid values.
    """
    return (PICASA_EPOCH + timedelta(days=float(raw))).isoformat(sep=" ", timespec="seconds")


@dataclass
class IniContent:
    images: dict[str, dict[str, str]] = field(default_factory=dict)
    albums: dict[str, dict[str, str]] = field(default_factory=dict)
    folder: dict[str, str] = field(default_factory=dict)


class _CaseSensitiveParser(configparser.ConfigParser):
    """ConfigParser that keeps property names as written (the default lowercases them)."""

    def optionxform(self, optionstr: str) -> str:
        return optionstr


def parse_ini(path: Path) -> IniContent:
    """Split one .picasa.ini into image sections, album definitions and folder metadata."""
    parser = _CaseSensitiveParser(interpolation=None, strict=False, delimiters=("=",))
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
class PicasaLibrary:
    """All Picasa metadata below one root folder.

    albums:  token -> {"token", "properties", "sourceIni"}
    folders: folder key -> {"relativePath", "properties", "sourceIni", "dateIso"?}
    files:   file key (see norm_path) -> {"fileName", "relativePath", "localPath",
             "properties", "sourceIni", "albumTokens"}
    """

    root: Path
    albums: dict[str, dict[str, Any]] = field(default_factory=dict)
    folders: dict[str, dict[str, Any]] = field(default_factory=dict)
    files: dict[str, dict[str, Any]] = field(default_factory=dict)
    ini_files: list[Path] = field(default_factory=list)
    warnings: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def scan(cls, root: Path) -> PicasaLibrary:
        """Read every .picasa.ini below root and resolve album memberships."""
        library = cls(root.resolve())
        for ini in sorted(library.root.rglob(INI_NAME)):
            library._add_ini(ini)
        library._resolve_album_tokens()
        return library

    # -- reading -------------------------------------------------------------

    def _add_ini(self, ini: Path) -> None:
        self.ini_files.append(ini)
        content = parse_ini(ini)
        if content.folder:
            self._add_folder(ini, content.folder)
        for token, props in content.albums.items():
            album = self.albums.setdefault(token, {"token": token, "properties": {}, "sourceIni": []})
            album["properties"].update(props)
            album["sourceIni"].append(str(ini))
        for filename, props in content.images.items():
            self._add_image(ini, filename, props)

    def _add_folder(self, ini: Path, metadata: dict[str, str]) -> None:
        relative_dir = ini.parent.relative_to(self.root).as_posix()
        folder = self.folders.setdefault(
            norm_path(relative_dir or "."),
            {
                "relativePath": relative_dir if relative_dir != "." else "",
                "properties": {},
                "sourceIni": [],
            },
        )
        folder["properties"].update(metadata)
        source_ini = ini.relative_to(self.root).as_posix()
        if source_ini not in folder["sourceIni"]:
            folder["sourceIni"].append(source_ini)
        raw_date = metadata.get("date")
        if raw_date:
            try:
                folder["dateIso"] = picasa_date_to_iso(raw_date)
            except (ValueError, OverflowError):
                self.warnings.append(
                    {
                        "type": "invalid_picasa_folder_date",
                        "relativePath": folder["relativePath"],
                        "value": raw_date,
                        "sourceIni": source_ini,
                    }
                )

    def _add_image(self, ini: Path, filename: str, props: dict[str, str]) -> None:
        # Image-section names are relative to the directory containing the INI.
        # That directory is part of the key, so equal filenames stay distinct.
        local_path = (ini.parent / filename).resolve()
        try:
            relative_path = local_path.relative_to(self.root)
        except ValueError:
            self.warnings.append(
                {
                    "type": "image_path_outside_source",
                    "fileName": filename,
                    "sourceIni": str(ini),
                    "resolvedPath": str(local_path),
                }
            )
            return
        record = self.files.setdefault(
            norm_path(relative_path),
            {
                "fileName": filename,
                "relativePath": relative_path.as_posix(),
                "localPath": str(local_path),
                "properties": {},
                "sourceIni": [],
            },
        )
        record["properties"].update(props)
        if str(ini) not in record["sourceIni"]:
            record["sourceIni"].append(str(ini))

    def _resolve_album_tokens(self) -> None:
        """Split each file's 'albums' property; done after all INIs are read."""
        for record in self.files.values():
            tokens = [t.strip() for t in record["properties"].get("albums", "").split(",") if t.strip()]
            record["albumTokens"] = tokens
            for token in tokens:
                if token not in self.albums:
                    self.warnings.append(
                        {
                            "type": "undefined_album_token",
                            "fileName": record["fileName"],
                            "relativePath": record["relativePath"],
                            "token": token,
                        }
                    )

    # -- queries -------------------------------------------------------------

    def album_name(self, token: str) -> str:
        album = self.albums.get(token)
        return album_display_name(token, album["properties"] if album else {})

    def album_names(self) -> dict[str, str]:
        return {token: self.album_name(token) for token in self.albums}

    def local_files(self) -> dict[str, Path]:
        """Map each Picasa file key to its existing, supported local file."""
        result: dict[str, Path] = {}
        for key, record in self.files.items():
            path = (self.root / record["relativePath"]).resolve()
            if not path.is_relative_to(self.root):
                LOG.warning("Ignoring image path outside source directory: %s", path)
            elif path.is_file() and path.suffix.casefold() in IMAGE_VIDEO_EXTENSIONS:
                result[key] = path
            else:
                LOG.warning("Local image file not found or unsupported: %s", path)
        return result

    def copy_original_ini(self, output: Path) -> None:
        """Copy every .picasa.ini byte-for-byte to output/original_ini, keeping the folder layout."""
        target_root = output / "original_ini"
        for source in self.ini_files:
            target = target_root / source.relative_to(self.root)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
