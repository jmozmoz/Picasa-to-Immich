"""Migration mode: match Picasa files to Immich assets, sync albums, write reports."""

from __future__ import annotations

import logging
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

from . import __version__
from .immich import ImmichClient
from .matching import AssetIndex, match_local_file
from .picasa import PicasaLibrary
from .settings import APP_NAME
from .util import json_write, timestamp

LOG = logging.getLogger(__name__)

REPORT_VERSION = 2
PICASA_EDIT_KEYS = ("backuphash", "rotate", "crop", "filters", "redo")
# Every album description written by this program starts with this text.
DESCRIPTION_PREFIX = f"Created by {APP_NAME}"


def album_description(name: str, properties: Mapping[str, Any], today: str) -> str:
    """Description for an album created by the migration: origin, plus Picasa description and location."""
    lines = [f'{DESCRIPTION_PREFIX} {__version__} from Picasa album "{name}" on {today}.']
    for key, label in (("description", "Picasa description"), ("location", "Picasa location")):
        value = str(properties.get(key, "")).strip()
        if value:
            lines.append(f"{label}: {value}")
    return "\n".join(lines)


@dataclass
class MatchResults:
    entries: list[dict[str, Any]] = field(default_factory=list)  # migration_report.json "matches"
    mapping: dict[str, Any] = field(default_factory=dict)  # asset_mapping.json
    archive: dict[str, Any] = field(default_factory=dict)  # metadata grouped by asset ID
    asset_ids_by_token: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    counts: Counter = field(default_factory=Counter)


@dataclass(frozen=True)
class MigrationOptions:
    """How a Migration runs; the defaults are a dry run with strict matching."""

    apply: bool = False  # actually change Immich; otherwise only report the planned changes
    allow_name_only: bool = False  # accept unique filename-only matches
    page_size: int = 500  # Immich search page size
    describe_albums: bool = True  # set a description on albums created in Immich


class Migration:
    """One migration run of a Picasa library into Immich.

    With options.apply=False (the default) nothing is changed in Immich; the
    planned changes are only reported.
    """

    def __init__(
        self, api: ImmichClient, library: PicasaLibrary, output: Path, options: MigrationOptions = MigrationOptions()
    ):
        self.api = api
        self.library = library
        self.output = output
        self.options = options
        self.today = time.strftime("%Y-%m-%d")
        self.warnings: list[dict[str, Any]] = list(library.warnings)

    def run(self) -> dict[str, Any]:
        """Read the Immich inventory, match, sync albums and write all reports."""
        version = self.api.server_version()
        LOG.info("Immich server: %s", version or "version endpoint unavailable")
        LOG.info("Retrieving existing Immich assets (this may take a while)")
        index = AssetIndex(self.api.list_assets(self.options.page_size))
        LOG.info("Retrieved %d Immich assets", len(index))
        existing_albums = self.api.list_albums()

        local_paths = self.library.local_files()
        matches = self.match_files(local_paths, index)
        albums = self.sync_albums(matches.asset_ids_by_token, existing_albums)
        report = self._report(version, matches, albums, len(local_paths), len(index))
        self.write_reports(matches, report)
        return report

    # -- matching ------------------------------------------------------------

    def match_files(self, local_paths: Mapping[str, Path], index: AssetIndex) -> MatchResults:
        """Match every Picasa file to an Immich asset and collect the report entries and archive."""
        results = MatchResults()
        for key, record in self.library.files.items():
            base = {"fileName": record["fileName"], "relativePath": record["relativePath"]}
            local_path = local_paths.get(key)
            if not local_path:
                results.entries.append({**base, "status": "unmatched", "reason": "local_file_not_found"})
                results.counts["unmatched"] += 1
                continue
            match = match_local_file(local_path, index, self.options.allow_name_only)
            results.counts[match.status] += 1
            if not match.asset:
                results.entries.append(
                    {
                        **base,
                        "localPath": str(local_path),
                        "status": match.status,
                        "reason": match.reason,
                        "candidateIds": [a.get("id") for a in match.candidates],
                    }
                )
                continue
            asset_id = str(match.asset["id"])
            results.entries.append(
                {
                    **base,
                    "localPath": str(local_path),
                    "immichAssetId": asset_id,
                    "status": match.status,
                    "method": match.reason,
                    "albumTokens": record["albumTokens"],
                }
            )
            results.mapping[key] = {
                **base,
                "localPath": str(local_path),
                "immichAssetId": asset_id,
                "originalFileName": match.asset.get("originalFileName"),
                "matchMethod": match.reason,
            }
            for token in record["albumTokens"]:
                results.asset_ids_by_token[token].add(asset_id)
            self._archive_match(results.archive, asset_id, match.asset, local_path, record)
        return results

    def _archive_match(
        self,
        archive: dict[str, Any],
        asset_id: str,
        asset: Mapping[str, Any],
        local_path: Path,
        record: Mapping[str, Any],
    ) -> None:
        """Merge a file's Picasa metadata into its asset record; one asset can have several local paths."""
        entry = archive.setdefault(
            asset_id,
            {
                "immichAssetId": asset_id,
                "originalFileName": asset.get("originalFileName"),
                "localPath": str(local_path),
                "localPaths": [],
                "picasa": {"allProperties": {}, "sourceIni": []},
                "picasaAlbums": [],
                "immich": {"editsApplied": [], "editsPending": ["rotate", "crop", "filters", "redo"]},
                "migration": {"status": "preserved", "version": 1},
            },
        )
        if str(local_path) not in entry["localPaths"]:
            entry["localPaths"].append(str(local_path))
        picasa = entry["picasa"]
        picasa["allProperties"].update(record["properties"])
        picasa["sourceIni"] = sorted(set(picasa["sourceIni"]) | set(record["sourceIni"]))
        for key in PICASA_EDIT_KEYS:
            picasa[key] = record["properties"].get(key)
        for token in record["albumTokens"]:
            membership = {"token": token, "name": self.library.album_name(token)}
            if membership not in entry["picasaAlbums"]:
                entry["picasaAlbums"].append(membership)

    # -- albums --------------------------------------------------------------

    def sync_albums(
        self, asset_ids_by_token: Mapping[str, set[str]], existing_albums: Iterable[Mapping[str, Any]]
    ) -> list[dict[str, Any]]:
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
        for token, album in self.library.albums.items():
            name = self.library.album_name(token)
            desired = sorted(asset_ids_by_token.get(token, set()))
            existing = by_name.get(name.casefold(), [])
            report = {
                "token": token,
                "name": name,
                "picasaProperties": album["properties"],
                "matchedAssetCount": len(desired),
                "action": "existing" if existing else "create",
                "addedAssetIds": [],
                "alreadyPresentAssetCount": 0,
            }
            if len(existing) > 1:
                self.warnings.append(
                    {
                        "type": "duplicate_immich_album_names",
                        "albumName": name,
                        "albumIds": [x.get("id") for x in existing],
                    }
                )
                report["action"] = "ambiguous_existing_album"
            elif existing:
                self._update_existing_album(str(existing[0]["id"]), desired, report)
            else:
                self._create_album(name, desired, album["properties"], report)
            reports.append(report)
        return reports

    def _update_existing_album(self, album_id: str, desired: list[str], report: dict[str, Any]) -> None:
        try:
            present = self.api.album_asset_ids(album_id, self.options.page_size)
        except (RuntimeError, OSError) as exc:  # HTTP error or network failure: skip this album
            self.warnings.append(
                {"type": "album_membership_read_failed", "albumName": report["name"], "error": str(exc)}
            )
            report["action"] = "skipped_membership_read_failed"
            return
        missing = [asset_id for asset_id in desired if asset_id not in present]
        report["alreadyPresentAssetCount"] = len(desired) - len(missing)
        report["addedAssetIds"] = missing
        report["action"] = "add_missing_members" if missing else "already_complete"
        if self.options.apply and missing:
            self.api.add_album_assets(album_id, missing)
            report["action"] = "updated_existing_album"

    def _create_album(
        self, name: str, desired: list[str], properties: Mapping[str, Any], report: dict[str, Any]
    ) -> None:
        report["addedAssetIds"] = desired
        description = album_description(name, properties, self.today) if self.options.describe_albums else ""
        if description:
            report["description"] = description
        if not self.options.apply:
            return
        created = self.api.create_album(name, desired, description)
        if not isinstance(created, dict) or not created.get("id"):
            raise RuntimeError(f"Album creation response did not contain an id for {name!r}: {created!r}")
        report["immichAlbumId"] = str(created["id"])
        report["action"] = "created"

    # -- reports -------------------------------------------------------------

    def _report(
        self,
        version: str | None,
        matches: MatchResults,
        albums: list[dict[str, Any]],
        local_path_count: int,
        asset_count: int,
    ) -> dict[str, Any]:
        counts = matches.counts
        library = self.library
        return {
            "scriptVersion": REPORT_VERSION,
            "createdAt": timestamp(),
            "mode": "apply" if self.options.apply else "dry-run",
            "immichServerVersion": version,
            "matches": matches.entries,
            "albums": albums,
            "warnings": self.warnings,
            "summary": {
                "iniFiles": len(library.ini_files),
                "picasaAlbumDefinitions": len(library.albums),
                "picasaImageSections": len(library.files),
                "distinctLocalImagePaths": local_path_count,
                "immichAssetsScanned": asset_count,
                "matchedAssets": counts["matched"] + counts["matched_low_confidence"],
                "highConfidenceMatches": counts["matched"],
                "lowConfidenceMatches": counts["matched_low_confidence"],
                "unmatched": counts["unmatched"],
                "ambiguous": counts["ambiguous"],
                "albumsProcessed": len(albums),
            },
        }

    def write_reports(self, matches: MatchResults, report: dict[str, Any]) -> None:
        json_write(self.output / "asset_mapping.json", matches.mapping)
        json_write(
            self.output / "picasa_migration_metadata.json",
            {"assets": matches.archive, "albums": self.library.albums, "folders": self.library.folders},
        )
        json_write(self.output / "migration_report.json", report)


def run_migration(
    api: ImmichClient,
    root: Path,
    output: Path,
    *,
    apply: bool = False,
    allow_name_only: bool = False,
    page_size: int = 500,
    describe_albums: bool = True,
) -> dict[str, Any]:
    """Scan root, archive the original INI files and run a Migration."""
    LOG.info("Scanning Picasa metadata under %s", root)
    library = PicasaLibrary.scan(root)
    library.copy_original_ini(output)
    LOG.info(
        "Found %d .picasa.ini files, %d folder metadata records, %d album definitions, %d image/video sections",
        len(library.ini_files),
        len(library.folders),
        len(library.albums),
        len(library.files),
    )
    options = MigrationOptions(
        apply=apply, allow_name_only=allow_name_only, page_size=page_size, describe_albums=describe_albums
    )
    return Migration(api, library, output, options).run()
