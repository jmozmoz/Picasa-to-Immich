import pytest

from picasa_immich import picasa
from picasa_immich.picasa import PicasaLibrary
from picasa_immich.util import norm_path
from tests.helpers import write


def test_parse_ini_splits_sections(tmp_path):
    ini = write(
        tmp_path / ".picasa.ini",
        (
            "[Picasa]\nname=Folder\n"
            "[.album:abc]\nname=Trip\nCustomProp=Keep\n"
            "[.album: ]\nname=no token\n"
            "[IMG.jpg]\nalbums=abc\nStar=yes\n"
        ),
    )
    content = picasa.parse_ini(ini)
    assert content.folder == {"name": "Folder"}
    assert content.albums == {"abc": {"name": "Trip", "CustomProp": "Keep"}}
    assert content.images == {"IMG.jpg": {"albums": "abc", "Star": "yes"}}


def test_parse_ini_merges_duplicate_sections(tmp_path):
    ini = write(tmp_path / ".picasa.ini", "[a.jpg]\nalbums=x\n[a.jpg]\nrotate=rotate(1)\n")
    assert picasa.parse_ini(ini).images == {"a.jpg": {"albums": "x", "rotate": "rotate(1)"}}


def test_parse_ini_tolerates_broken_file(tmp_path):
    ini = write(tmp_path / ".picasa.ini", "no section header\n")
    assert picasa.parse_ini(ini) == picasa.IniContent()


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("0", "1899-12-30 00:00:00"),
        ("43831.980718", "2020-01-01 23:32:14"),
    ],
)
def test_picasa_date_to_iso(raw, expected):
    assert picasa.picasa_date_to_iso(raw) == expected


def test_picasa_date_to_iso_rejects_text():
    with pytest.raises(ValueError):
        picasa.picasa_date_to_iso("yesterday")


@pytest.mark.parametrize(
    ("props", "expected"),
    [
        ({"name": "  Trip  "}, "Trip"),
        ({"name": ""}, "Picasa album tok"),
        ({}, "Picasa album tok"),
    ],
)
def test_album_display_name(props, expected):
    assert picasa.album_display_name("tok", props) == expected


def test_norm_path_is_case_and_separator_insensitive():
    assert norm_path("Dir\\Sub\\IMG.JPG") == norm_path("dir/sub/img.jpg")


def test_scan_picasa(picasa_tree):
    data = PicasaLibrary.scan(picasa_tree)

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

    assert data.warnings == [
        {"type": "undefined_album_token", "fileName": "b.jpg", "relativePath": "2020/b.jpg", "token": "tokX"}
    ]


def test_scan_picasa_warns_about_bad_date_and_outside_paths(tmp_path):
    root = tmp_path / "root"
    write(root / "x" / ".picasa.ini", "[Picasa]\ndate=soon\n[../../escape.jpg]\nalbums=a\n")
    data = PicasaLibrary.scan(root)
    assert [w["type"] for w in data.warnings] == ["invalid_picasa_folder_date", "image_path_outside_source"]
    assert not data.files
    assert "dateIso" not in data.folders["x"]


def test_scan_picasa_root_folder_metadata(tmp_path):
    write(tmp_path / ".picasa.ini", "[Picasa]\nname=Root\n")
    data = PicasaLibrary.scan(tmp_path)
    assert data.folders["."]["relativePath"] == ""


def test_build_local_index_skips_missing_and_unsupported(picasa_tree):
    data = PicasaLibrary.scan(picasa_tree)
    index = data.local_files()
    assert set(index) == {"2020/a.jpg", "2020/b.jpg", "2020/sub/a.jpg", "2021/v.mp4"}
    assert index["2021/v.mp4"] == (picasa_tree / "2021" / "v.mp4").resolve()


def test_album_name_for_defined_and_undefined_tokens(picasa_tree):
    library = PicasaLibrary.scan(picasa_tree)
    assert library.album_name("tok1") == "Holiday 2020"
    assert library.album_name("tok3") == "Picasa album tok3"
    assert library.album_name("unknown") == "Picasa album unknown"
    assert library.album_names() == {"tok1": "Holiday 2020", "tok2": "Best of", "tok3": "Picasa album tok3"}


def test_copy_original_ini_keeps_layout_and_bytes(picasa_tree, tmp_path):
    library = PicasaLibrary.scan(picasa_tree)
    library.copy_original_ini(tmp_path / "out")
    copies = sorted(
        p.relative_to(tmp_path / "out" / "original_ini").as_posix() for p in (tmp_path / "out").rglob(".picasa.ini")
    )
    assert copies == ["2020/.picasa.ini", "2020/sub/.picasa.ini", "2021/.picasa.ini"]
    sub = tmp_path / "out" / "original_ini" / "2020" / "sub" / ".picasa.ini"
    assert sub.read_bytes() == (picasa_tree / "2020" / "sub" / ".picasa.ini").read_bytes()
