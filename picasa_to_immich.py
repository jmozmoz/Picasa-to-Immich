#!/usr/bin/env python3
"""Run picasa-to-immich from a source checkout without installing it.

After `pip install .` the same program is available as the `picasa-to-immich`
command. The code lives in the picasa_immich package next to this file.
"""
from picasa_immich.cli import run

if __name__ == "__main__":
    run()
