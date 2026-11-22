"""Package layout: module independence and the picasa_to_immich.py entry script."""
import subprocess
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent


def test_default_env_file_is_in_project_folder():
    # Checked in a fresh interpreter, since conftest replaces DEFAULT_ENV_FILE.
    code = "from picasa_immich import settings; print(settings.DEFAULT_ENV_FILE)"
    out = subprocess.run([sys.executable, "-c", code], cwd=PROJECT, capture_output=True, text=True, check=True)
    assert Path(out.stdout.strip()) == PROJECT / ".env"


def test_picasa_and_matching_do_not_need_immich_or_requests():
    code = ("import sys; import picasa_immich.picasa, picasa_immich.matching, picasa_immich.scan_report; "
            "print(sorted(m for m in sys.modules if m == 'requests' or m.endswith('.immich')))")
    out = subprocess.run([sys.executable, "-c", code], cwd=PROJECT, capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "[]"


def test_entry_script_runs_from_another_folder(tmp_path):
    out = subprocess.run([sys.executable, str(PROJECT / "picasa_to_immich.py"), "--help"],
                         cwd=tmp_path, capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert out.stdout.startswith("usage: picasa_to_immich.py")
    assert "--env-file" in out.stdout


def test_entry_script_scan_only(picasa_tree, tmp_path):
    output = tmp_path / "out"
    out = subprocess.run([sys.executable, str(PROJECT / "picasa_to_immich.py"), str(picasa_tree),
                          "--scan-only", "--output", str(output)], cwd=tmp_path, capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert (output / "album_file_list.json").is_file()
