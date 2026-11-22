#!/usr/bin/env python3
"""Scan Picasa albums or migrate their memberships to Immich.

The code lives in the picasa_immich package next to this file; see
picasa_immich/cli.py for the command-line options, or run with --help.
"""
from picasa_immich.cli import run

if __name__ == "__main__":
    run()
