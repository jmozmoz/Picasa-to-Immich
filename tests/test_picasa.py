import json

import pytest

import picasa_to_immich as p2i
from conftest import write


def test_parse_ini_splits_sections(tmp_path):
    ini = write(tmp_path / ".picasa.ini", (
        "[Picasa]\nname=Folder\n"
        "[.album:abc]\nname=Trip\nCustomProp=Keep\n"
        "[.album: ]\nname=no token\n"
        "[IMG.jpg]\nalbums=abc\nStar=yes\n"
    ))
    content = p2i.parse_ini(ini)
    assert content.folder == {"name": "Folder"}
    assert content.albums == {"abc": {"name": "Trip", "CustomProp": "Keep"}}
    assert content.images == {"IMG.jpg": {"albums": "abc", "Star": "yes"}}


def test_parse_ini_merges_duplicate_sections(tmp_path):
    ini = write(tmp_path / ".picasa.ini", "[a.jpg]\nalbums=x\n[a.jpg]\nrotate=rotate(1)\n")
    assert p2i.parse_ini(ini).images == {"a.jpg": {"albums": "x", "rotate": "rotate(1)"}}


def test_parse_ini_tolerates_broken_file(tmp_path):
    ini = write(tmp_path / ".picasa.ini", "no section header\n")
    assert p2i.parse_ini(ini) == p2i.IniContent()


@pytest.mark.parametrize(("raw", "expected"), [
    ("0", "1899-12-30 00:00:00"),
    ("43831.980718", "2020-01-01 23:32:14"),
])
def test_picasa_date_to_iso(raw, expected):
    assert p2i.picasa_date_to_iso(raw) == expected


def test_picasa_date_to_iso_rejects_text():
    with pytest.raises(ValueError):
        p2i.picasa_date_to_iso("yesterday")


@pytest.mark.parametrize(("props", "expected"), [
    ({"name": "  Trip  "}, "Trip"),
    ({"name": ""}, "Picasa album tok"),
    ({}, "Picasa album tok"),
])
def test_album_display_name(props, expected):
    assert p2i.album_display_name("tok", props) == expected


def test_norm_path_is_case_and_separator_insensitive():
    assert p2i.norm_path("Dir\\Sub\\IMG.JPG") == p2i.norm_path("dir/sub/img.jpg")


def test_scan_picasa(picasa_tree):
    data = p2i.scan_picasa(picasa_tree)

    assert len(data.ini_files) == 3
    assert set(data.albums) == {"tok1", "tok2", "tok3"}
    assert data.albums["tok1"]["properties"] == {"name": "Holiday 2020", "token": "tok1"}

    # Same filename in two directories stays two distinct files.
    assert {"2020/a.jpg", "2020/sub/a.jpg"} <= {f["relativePath"] for f in data.files.values()}
    a = data.files["2020/a.jpg"]
    assert a["albumTokens"] == ["tok1", "tok2"]
    assert a["properties"]["rotate"] == "rotate(1)"
    assert data.files["2020/b.jpg"]["albumTokens"] == ["tok1", "tokX"]

    folder = data.folders["2020"]
    assert folder["properties"]["name"] == "Holiday folder"
    assert folder["dateIso"] == "2020-01-01 23:32:14"
    assert folder["sourceIni"] == ["2020/.picasa.ini"]

    assert data.warnings == [{"type": "undefined_album_token", "fileName": "b.jpg",
                              "relativePath": "2020/b.jpg", "token": "tokX"}]


def test_scan_picasa_warns_about_bad_date_and_outside_paths(tmp_path):
    root = tmp_path / "root"
    write(root / "x" / ".picasa.ini", "[Picasa]\ndate=soon\n[../../escape.jpg]\nalbums=a\n")
    data = p2i.scan_picasa(root)
    assert [w["type"] for w in data.warnings] == ["invalid_picasa_folder_date", "image_path_outside_source"]
    assert data.files == {}
    assert "dateIso" not in data.folders["x"]


def test_scan_picasa_root_folder_metadata(tmp_path):
    write(tmp_path / ".picasa.ini", "[Picasa]\nname=Root\n")
    data = p2i.scan_picasa(tmp_path)
    assert data.folders["."]["relativePath"] == ""


def test_build_local_index_skips_missing_and_unsupported(picasa_tree):
    data = p2i.scan_picasa(picasa_tree)
    index = p2i.build_local_index(picasa_tree, data)
    assert set(index) == {"2020/a.jpg", "2020/b.jpg", "2020/sub/a.jpg", "2021/v.mp4"}
    assert index["2021/v.mp4"] == (picasa_tree / "2021" / "v.mp4").resolve()


def test_build_album_file_list(picasa_tree):
    data = p2i.scan_picasa(picasa_tree)
    result = p2i.build_album_file_list(picasa_tree, data, p2i.build_local_index(picasa_tree, data))
    albums = {a["token"]: a for a in result["albums"]}

    assert albums["tok1"]["files"] == ["2020/a.jpg", "2020/b.jpg", "2021/v.mp4"]
    assert albums["tok1"]["missingFiles"] == ["2020/missing.jpg"]
    assert albums["tok2"]["files"] == ["2020/a.jpg", "2020/sub/a.jpg"]
    assert albums["tok3"] == {"token": "tok3", "name": "Picasa album tok3", "files": ["2021/v.mp4"],
                              "missingFiles": ["2021/notes.txt"], "fileCount": 1}
    assert (result["iniFileCount"], result["albumCount"], result["imageSections"]) == (3, 3, 6)


def test_album_file_list_text():
    text = p2i.album_file_list_text([
        {"name": "Trip", "token": "t1", "files": ["a.jpg", "b.jpg"], "missingFiles": ["c.jpg"]},
        {"name": "Empty", "token": "t2", "files": [], "missingFiles": []},
    ])
    assert text == ("[Trip] (token: t1)\na.jpg\nb.jpg\n# Missing or unsupported files:\n# c.jpg\n\n"
                    "[Empty] (token: t2)\n")


def test_run_scan_only_writes_both_files(picasa_tree, tmp_path):
    output = tmp_path / "out"
    p2i.run_scan_only(picasa_tree, output)
    listing = json.loads((output / "album_file_list.json").read_text(encoding="utf-8"))
    assert listing["albumCount"] == 3
    assert (output / "album_file_list.txt").read_text(encoding="utf-8").startswith("[Holiday 2020] (token: tok1)")
