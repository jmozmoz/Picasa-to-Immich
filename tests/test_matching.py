import hashlib

import pytest

import picasa_to_immich as p2i
from conftest import b64_sha1, external_asset, uploaded_asset

CONTENT = b"local file content"
SHA1 = hashlib.sha1(CONTENT).hexdigest()
SIZE = len(CONTENT)


def match(assets, filename="IMG.jpg", allow_name_only=False):
    return p2i.match_asset(SHA1, SIZE, filename, p2i.AssetIndex(assets), allow_name_only)


def test_checksum_forms():
    assert p2i.checksum_forms(SHA1.upper()) == {SHA1, b64_sha1(CONTENT)}


def test_sha1_file(tmp_path):
    path = tmp_path / "f.bin"
    path.write_bytes(CONTENT)
    assert p2i.sha1_file(path, chunk_size=3) == (SHA1, SIZE)


@pytest.mark.parametrize(("asset", "expected"), [
    ({"fileSize": 5}, 5),
    ({"originalFileSize": 6.0}, 6),
    ({"exifInfo": {"fileSizeInByte": 7}}, 7),
    ({"exifInfo": {"fileSizeInBytes": 8}}, 8),
    ({"exifInfo": {"fileSizeInByte": None}}, None),
    ({}, None),
])
def test_remote_size(asset, expected):
    assert p2i.remote_size(asset) == expected


def test_path_based_checksum_is_detected_and_ignored():
    asset = external_asset("e", "IMG.jpg", "/lib/IMG.jpg", SIZE)
    assert p2i.is_path_based_checksum(asset)
    assert p2i.remote_checksum(asset) is None


def test_content_checksum_is_kept():
    asset = uploaded_asset("u", "IMG.jpg", CONTENT)
    assert not p2i.is_path_based_checksum(asset)
    assert p2i.remote_checksum(asset) == b64_sha1(CONTENT)


def test_hex_checksum_with_path_hash_is_detected():
    path_hash = hashlib.sha1(b"path:/lib/x.jpg").hexdigest()
    assert p2i.is_path_based_checksum({"checksum": path_hash, "originalPath": "/lib/x.jpg"})


def test_checksum_without_original_path_counts_as_content():
    assert not p2i.is_path_based_checksum({"checksum": "abc"})


def test_match_by_sha1_ignores_filename():
    result = match([uploaded_asset("u", "renamed.jpg", CONTENT)])
    assert (result.status, result.reason, result.asset["id"]) == ("matched", "sha1", "u")


def test_match_by_hex_sha1():
    result = match([{"id": "h", "originalFileName": "x.jpg", "checksum": SHA1}])
    assert (result.status, result.asset["id"]) == ("matched", "h")


def test_multiple_sha1_matches_are_ambiguous_in_inventory_order():
    assets = [uploaded_asset("u2", "a.jpg", CONTENT), uploaded_asset("u1", "b.jpg", CONTENT)]
    result = match(assets)
    assert (result.status, result.reason) == ("ambiguous", "multiple_sha1_matches")
    assert [a["id"] for a in result.candidates] == ["u2", "u1"]


def test_external_library_asset_matches_by_name_and_size():
    result = match([external_asset("e", "img.JPG", "/lib/img.JPG", SIZE)])
    assert (result.status, result.reason) == ("matched", "filename_and_size_path_based_server_checksum")


def test_asset_without_checksum_matches_by_name_and_size():
    result = match([{"id": "n", "originalFileName": "IMG.jpg", "fileSize": SIZE}])
    assert (result.status, result.reason) == ("matched", "filename_and_size_no_server_checksum")


def test_same_name_with_different_content_checksum_is_rejected():
    other = uploaded_asset("u", "IMG.jpg", b"other content")
    other["fileSize"] = SIZE
    result = match([other, external_asset("e", "IMG.jpg", "/lib/IMG.jpg", SIZE)])
    assert (result.status, result.reason) == ("unmatched", "server_checksum_mismatch")
    assert [a["id"] for a in result.candidates] == ["u"]


def test_duplicate_external_assets_are_ambiguous():
    assets = [external_asset("x", "IMG.jpg", "/x/IMG.jpg", SIZE), external_asset("y", "IMG.jpg", "/y/IMG.jpg", SIZE)]
    result = match(assets)
    assert (result.status, result.reason) == ("ambiguous", "multiple_filename_size_matches")


def test_wrong_size_is_unmatched_unless_name_only_is_allowed():
    assets = [external_asset("e", "IMG.jpg", "/lib/IMG.jpg", SIZE + 1)]
    assert match(assets).reason == "filename_found_but_size_unavailable_or_different"
    result = match(assets, allow_name_only=True)
    assert (result.status, result.reason) == ("matched_low_confidence", "filename_only_explicitly_allowed")


def test_name_only_with_several_candidates_is_ambiguous():
    assets = [external_asset("x", "IMG.jpg", "/x/IMG.jpg", 1), external_asset("y", "IMG.jpg", "/y/IMG.jpg", 2)]
    result = match(assets, allow_name_only=True)
    assert (result.status, result.reason) == ("ambiguous", "multiple_filename_matches")


def test_no_candidate():
    result = match([uploaded_asset("u", "other.jpg", b"x")])
    assert (result.status, result.reason, result.candidates) == ("unmatched", "no_candidate", [])


def test_match_local_file_reads_the_file(tmp_path):
    path = tmp_path / "IMG.jpg"
    path.write_bytes(CONTENT)
    result = p2i.match_local_file(path, p2i.AssetIndex([uploaded_asset("u", "x.jpg", CONTENT)]), False)
    assert result.asset["id"] == "u"


def test_match_local_file_reports_unreadable_file(tmp_path):
    result = p2i.match_local_file(tmp_path / "gone.jpg", p2i.AssetIndex([]), False)
    assert result.status == "unmatched"
    assert result.reason.startswith("cannot_read_local_file")


def test_asset_index_does_not_modify_assets():
    asset = external_asset("e", "IMG.jpg", "/lib/IMG.jpg", SIZE)
    before = dict(asset)
    p2i.AssetIndex([asset])
    assert asset == before
