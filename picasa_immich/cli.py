"""Scan Picasa albums or migrate their memberships to Immich.

Use --scan-only to export album membership as source-relative file paths; this
mode does not connect to Immich and needs no API key.
Otherwise, dry-run is the default. Use --apply to create albums / add existing
assets.

Every run writes to a new folder OUTPUT/<date>T<time>-<scan|dryrun|apply>, so
earlier reports are never overwritten. In migration mode the original
.picasa.ini files are copied byte-for-byte to its original_ini subfolder, and
all Picasa properties (including unknown ones) are archived in JSON.
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

from . import __version__, settings
from .immich import ImmichAPI
from .migration import run_migration
from .scan_report import run_scan_only
from .util import new_run_dir

LOG = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    """The command-line options; defaults and help texts are documented in the README."""
    parser = argparse.ArgumentParser(
        prog=settings.APP_NAME, description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("source", type=Path, help="Root folder containing Picasa folders and .picasa.ini files")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("picasa_migration"),
        help="Output folder; each run writes to a new timestamped subfolder (default: picasa_migration)",
    )
    parser.add_argument(
        "--scan-only",
        action="store_true",
        help="Only scan Picasa folders and write album file lists; no Immich connection or API key required",
    )
    parser.add_argument(
        "--url", help="Immich base URL, e.g. http://192.168.1.100:2283 (or IMMICH_URL in the environment or .env)"
    )
    parser.add_argument("--api-key", help="API key (or IMMICH_API_KEY in the environment or .env)")
    parser.add_argument(
        "--env-file",
        type=Path,
        help="File with IMMICH_URL / IMMICH_API_KEY lines. Default: the first existing of "
        + " and ".join(str(p) for p in settings.default_env_files()),
    )
    parser.add_argument(
        "--apply", action="store_true", help="Actually create albums and add assets; default is dry-run"
    )
    parser.add_argument(
        "--allow-name-only", action="store_true", help="Allow unique filename-only matches (lower confidence)"
    )
    parser.add_argument("--no-verify-tls", action="store_true", help="Disable TLS certificate verification")
    parser.add_argument("--page-size", type=int, default=500, help="Immich search page size (default: 500)")
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    parser.add_argument(
        "--debug-api", action="store_true", help="Log every Immich API request (method, URL, body) and response status"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the program with the given arguments (default: sys.argv) and return the exit code.

    Argument errors exit via argparse with code 2; other errors propagate (see run()).
    """
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
        result = run_scan_only(root, new_run_dir(output, "scan"))
        LOG.info(
            "Found %d .picasa.ini files, %d folder metadata records, %d album definitions, %d image/video sections",
            result["iniFileCount"],
            result["folderMetadataCount"],
            result["albumCount"],
            result["imageSections"],
        )
        return 0

    try:
        credentials = settings.resolve_credentials(args.url, args.api_key, args.env_file)
    except ValueError as exc:
        parser.error(str(exc))
    if credentials.env_file_used:
        LOG.info("Read settings from %s", credentials.env_file_used)
    if not credentials.url or not credentials.api_key:
        parser.error(
            "Set --url and --api-key, or IMMICH_URL and IMMICH_API_KEY as environment variables "
            "or in a .env file (not needed with --scan-only)"
        )
    if args.page_size < 1:
        parser.error("--page-size must be positive")

    api = ImmichAPI(credentials.url, credentials.api_key, args.timeout, not args.no_verify_tls, debug=args.debug_api)
    run_dir = new_run_dir(output, "apply" if args.apply else "dryrun")
    report = run_migration(
        api, root, run_dir, apply=args.apply, allow_name_only=args.allow_name_only, page_size=args.page_size
    )
    LOG.info("Finished in %s mode", "APPLY" if args.apply else "DRY-RUN")
    LOG.info("Summary: %s", json.dumps(report["summary"], ensure_ascii=False))
    LOG.info("Reports written to %s", run_dir)
    if not args.apply:
        LOG.info("No Immich changes were made. Review migration_report.json, then rerun with --apply.")
    return 0


def run() -> None:
    """Entry point: like main(), but turns errors into exit codes."""
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        LOG.error("Interrupted")
        raise SystemExit(130) from None
    except Exception as exc:
        LOG.exception("Migration stopped: %s", exc)
        raise SystemExit(1) from exc
