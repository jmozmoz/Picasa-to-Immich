"""Scan-only mode: album file lists."""
import json

from picasa_immich import scan_report
from picasa_immich.picasa import PicasaLibrary


def test_build_album_file_list(picasa_tree):
    data = PicasaLibrary.scan(picasa_tree)
    result = scan_report.build_album_file_list(data, data.local_files())
    albums = {a["token"]: a for a in result["albums"]}

    assert albums["tok1"]["files"] == ["2020/a.jpg", "2020/b.jpg", "2021/v.mp4"]
    assert albums["tok1"]["missingFiles"] == ["2020/missing.jpg"]
    assert albums["tok2"]["files"] == ["2020/a.jpg", "2020/sub/a.jpg"]
    assert albums["tok3"] == {"token": "tok3", "name": "Picasa album tok3", "files": ["2021/v.mp4"],
                              "missingFiles": ["2021/notes.txt"], "fileCount": 1}
    assert (result["iniFileCount"], result["albumCount"], result["imageSections"]) == (3, 3, 6)


def test_album_file_list_text():
    text = scan_report.album_file_list_text([
        {"name": "Trip", "token": "t1", "files": ["a.jpg", "b.jpg"], "missingFiles": ["c.jpg"]},
        {"name": "Empty", "token": "t2", "files": [], "missingFiles": []},
    ])
    assert text == ("[Trip] (token: t1)\na.jpg\nb.jpg\n# Missing or unsupported files:\n# c.jpg\n\n"
                    "[Empty] (token: t2)\n")


def test_run_scan_only_writes_both_files(picasa_tree, tmp_path):
    output = tmp_path / "out"
    scan_report.run_scan_only(picasa_tree, output)
    listing = json.loads((output / "album_file_list.json").read_text(encoding="utf-8"))
    assert listing["albumCount"] == 3
    assert (output / "album_file_list.txt").read_text(encoding="utf-8").startswith("[Holiday 2020] (token: tok1)")
