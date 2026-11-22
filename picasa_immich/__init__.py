"""Scan Picasa albums or migrate their memberships to an existing Immich library.

Modules:
    settings     .env file and Immich URL/API-key resolution
    util         small shared helpers (JSON output, run folders, path keys, timestamps)
    picasa       reading .picasa.ini files (no Immich dependency)
    scan_report  scan-only output: album file lists
    matching     matching local files to Immich assets (no network)
    immich       Immich REST API client
    migration    matching, album sync and migration reports
    cli          command-line interface
"""

from importlib.metadata import PackageNotFoundError, version

try:
    # Set from the git tag by setuptools-scm when the package is installed
    # (in an editable install: at install time; reinstall to refresh it).
    __version__ = version("picasa-to-immich")
except PackageNotFoundError:  # running from a checkout that was never installed
    __version__ = "0+unknown"
