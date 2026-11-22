"""Shared helpers: per-run output folders."""

import time

from picasa_immich.util import new_run_dir

NOON = time.mktime((2026, 11, 22, 12, 5, 9, 0, 0, -1))


def test_new_run_dir_name_and_creation(tmp_path):
    run_dir = new_run_dir(tmp_path / "out", "apply", now=NOON)
    assert run_dir == tmp_path / "out" / "2026-11-22T120509-apply"
    assert run_dir.is_dir() and not any(run_dir.iterdir())


def test_new_run_dir_never_reuses_a_folder(tmp_path):
    names = [new_run_dir(tmp_path, "dryrun", now=NOON).name for _ in range(3)]
    assert names == ["2026-11-22T120509-dryrun", "2026-11-22T120509-dryrun-2", "2026-11-22T120509-dryrun-3"]


def test_new_run_dir_keeps_existing_content(tmp_path):
    first = new_run_dir(tmp_path, "scan", now=NOON)
    (first / "album_file_list.json").write_text("{}", encoding="utf-8")
    second = new_run_dir(tmp_path, "scan", now=NOON)
    assert second != first
    assert (first / "album_file_list.json").read_text(encoding="utf-8") == "{}"
