"""End-to-end tests of migration mode and the command line, using a fake Immich."""

import json
import re

import pytest

from picasa_immich import __version__, cli, migration
from tests.helpers import FakeImmich, external_asset, only_run_dir, uploaded_asset


def inventory():
    """Immich assets for the picasa_tree fixture."""
    return [
        external_asset("asset-a", "a.jpg", "/lib/2020/a.jpg", len(b"content-a")),
        uploaded_asset("asset-b", "renamed-b.jpg", b"content-b"),
        external_asset("asset-sub-a", "a.jpg", "/lib/2020/sub/a.jpg", len(b"content-sub-a")),
        # v.mp4 has two identical external copies -> ambiguous
        external_asset("asset-v1", "v.mp4", "/lib/2021/v.mp4", len(b"content-v")),
        external_asset("asset-v2", "v.mp4", "/copy/v.mp4", len(b"content-v")),
    ]


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_dry_run_reports_without_changes(picasa_tree, tmp_path):
    api = FakeImmich(
        inventory(), albums=[{"id": "alb-best", "albumName": "best OF"}], members={"alb-best": {"asset-a"}}
    )
    output = tmp_path / "out"
    report = migration.run_migration(api, picasa_tree, output)

    assert not api.created and not api.added
    assert report["mode"] == "dry-run"
    statuses = {m["relativePath"]: (m["status"], m.get("reason") or m.get("method")) for m in report["matches"]}
    assert statuses == {
        "2020/a.jpg": ("matched", "filename_and_size_path_based_server_checksum"),
        "2020/b.jpg": ("matched", "sha1"),
        "2020/sub/a.jpg": ("matched", "filename_and_size_path_based_server_checksum"),
        "2020/missing.jpg": ("unmatched", "local_file_not_found"),
        "2021/v.mp4": ("ambiguous", "multiple_filename_size_matches"),
        "2021/notes.txt": ("unmatched", "local_file_not_found"),
    }
    albums = {a["token"]: a for a in report["albums"]}
    assert (albums["tok1"]["action"], albums["tok1"]["addedAssetIds"]) == ("create", ["asset-a", "asset-b"])
    # Existing album found case-insensitively; asset-a is already a member.
    assert albums["tok2"]["action"] == "add_missing_members"
    assert (albums["tok2"]["addedAssetIds"], albums["tok2"]["alreadyPresentAssetCount"]) == (["asset-sub-a"], 1)
    assert (albums["tok3"]["name"], albums["tok3"]["matchedAssetCount"]) == ("Picasa album tok3", 0)

    assert report["summary"] == {
        "iniFiles": 3,
        "picasaAlbumDefinitions": 3,
        "picasaImageSections": 6,
        "distinctLocalImagePaths": 4,
        "immichAssetsScanned": 5,
        "matchedAssets": 3,
        "highConfidenceMatches": 3,
        "lowConfidenceMatches": 0,
        "unmatched": 2,
        "ambiguous": 1,
        "albumsProcessed": 3,
    }
    assert [w["type"] for w in report["warnings"]] == ["undefined_album_token"]
    assert load(output / "migration_report.json")["summary"] == report["summary"]


def test_output_files(picasa_tree, tmp_path):
    output = tmp_path / "out"
    migration.run_migration(FakeImmich(inventory()), picasa_tree, output)

    mapping = load(output / "asset_mapping.json")
    assert mapping["2020/a.jpg"]["immichAssetId"] == "asset-a"
    assert mapping["2020/b.jpg"]["matchMethod"] == "sha1"

    metadata = load(output / "picasa_migration_metadata.json")
    asset_a = metadata["assets"]["asset-a"]
    assert asset_a["picasa"]["rotate"] == "rotate(1)"
    assert asset_a["picasaAlbums"] == [{"token": "tok1", "name": "Holiday 2020"}, {"token": "tok2", "name": "Best of"}]
    assert metadata["assets"]["asset-b"]["picasaAlbums"][1] == {"token": "tokX", "name": "Picasa album tokX"}
    assert set(metadata["albums"]) == {"tok1", "tok2", "tok3"}
    assert metadata["contacts"]["c1"]["name"] == "Ann Example"

    original = output / "original_ini" / "2020" / ".picasa.ini"
    assert original.read_bytes() == (picasa_tree / "2020" / ".picasa.ini").read_bytes()


def test_one_asset_referenced_by_several_local_files(tmp_path):
    root = tmp_path / "root"
    for folder, token in (("x", "t1"), ("y", "t2")):
        (root / folder).mkdir(parents=True)
        (root / folder / "same.jpg").write_bytes(b"identical")
        (root / folder / ".picasa.ini").write_text(
            f"[.album:{token}]\nname=Album {token}\n[same.jpg]\nalbums={token}\n", encoding="utf-8"
        )
    api = FakeImmich([uploaded_asset("one", "same.jpg", b"identical")])
    output = tmp_path / "out"
    migration.run_migration(api, root, output, apply=True)

    assert sorted(api.created) == [("Album t1", ["one"]), ("Album t2", ["one"])]
    record = load(output / "picasa_migration_metadata.json")["assets"]["one"]
    assert len(record["localPaths"]) == 2
    assert [m["token"] for m in record["picasaAlbums"]] == ["t1", "t2"]


def test_apply_creates_and_updates_albums(picasa_tree, tmp_path):
    api = FakeImmich(inventory(), albums=[{"id": "alb-best", "albumName": "Best of"}])
    report = migration.run_migration(api, picasa_tree, tmp_path / "out", apply=True)

    assert api.created == [("Holiday 2020", ["asset-a", "asset-b"]), ("Picasa album tok3", [])]
    assert api.added == [("alb-best", ["asset-a", "asset-sub-a"])]
    actions = {a["token"]: a["action"] for a in report["albums"]}
    assert actions == {"tok1": "created", "tok2": "updated_existing_album", "tok3": "created"}
    assert report["albums"][0]["immichAlbumId"] == "new-1"


def test_album_description_text():
    props = {"name": "Trip", "location": " Rome, Italy ", "description": "Summer holiday"}
    assert migration.album_description("Trip", props, "2026-11-22") == (
        f'Created by picasa-to-immich {__version__} from Picasa album "Trip" on 2026-11-22.\n'
        "Picasa description: Summer holiday\n"
        "Picasa location: Rome, Italy"
    )
    assert migration.album_description("Trip", {"location": "  "}, "2026-11-22").count("\n") == 0


def test_created_albums_get_a_description(picasa_tree, tmp_path):
    api = FakeImmich(inventory(), albums=[{"id": "alb-best", "albumName": "Best of"}])
    report = migration.run_migration(api, picasa_tree, tmp_path / "out", apply=True)

    pattern = r'Created by picasa-to-immich \S+ from Picasa album "Holiday 2020" on \d{4}-\d{2}-\d{2}\.'
    assert re.fullmatch(pattern, api.descriptions["Holiday 2020"])
    assert set(api.descriptions) == {"Holiday 2020", "Picasa album tok3"}  # not the existing "Best of"
    by_token = {a["token"]: a for a in report["albums"]}
    assert by_token["tok1"]["description"] == api.descriptions["Holiday 2020"]
    assert "description" not in by_token["tok2"]


def test_dry_run_reports_the_planned_description(picasa_tree, tmp_path):
    report = migration.run_migration(FakeImmich(inventory()), picasa_tree, tmp_path / "out")
    assert report["albums"][0]["description"].startswith(migration.DESCRIPTION_PREFIX)


def test_album_description_can_be_disabled(picasa_tree, tmp_path):
    api = FakeImmich(inventory())
    report = migration.run_migration(api, picasa_tree, tmp_path / "out", apply=True, describe_albums=False)
    assert set(api.descriptions.values()) == {""}
    assert not any("description" in album for album in report["albums"])


def test_main_no_album_description(picasa_tree, tmp_path, monkeypatch):
    api = FakeImmich(inventory())
    monkeypatch.setattr(cli, "ImmichAPI", lambda *args, **kwargs: api)
    args = [str(picasa_tree), "--output", str(tmp_path / "out"), "--url", "http://x", "--api-key", "k"]
    assert cli.main([*args, "--apply", "--no-album-description"]) == 0
    assert api.created and set(api.descriptions.values()) == {""}


def test_complete_album_is_left_alone(picasa_tree, tmp_path):
    api = FakeImmich(
        inventory(), albums=[{"id": "alb", "albumName": "Best of"}], members={"alb": {"asset-a", "asset-sub-a"}}
    )
    report = migration.run_migration(api, picasa_tree, tmp_path / "out", apply=True)
    assert not api.added
    assert report["albums"][1]["action"] == "already_complete"


def test_duplicate_and_unreadable_albums_are_skipped(picasa_tree, tmp_path):
    api = FakeImmich(
        inventory(),
        albums=[
            {"id": "d1", "albumName": "Holiday 2020"},
            {"id": "d2", "albumName": "Holiday 2020"},
            {"id": "bad", "albumName": "Best of"},
        ],
        failing_albums={"bad"},
    )
    report = migration.run_migration(api, picasa_tree, tmp_path / "out", apply=True)

    actions = {a["token"]: a["action"] for a in report["albums"]}
    assert actions["tok1"] == "ambiguous_existing_album"
    assert actions["tok2"] == "skipped_membership_read_failed"
    assert not api.added and api.created == [("Picasa album tok3", [])]
    assert {w["type"] for w in report["warnings"]} == {
        "undefined_album_token",
        "duplicate_immich_album_names",
        "album_membership_read_failed",
    }


def test_create_album_without_id_is_an_error(picasa_tree, tmp_path, monkeypatch):
    api = FakeImmich(inventory())
    monkeypatch.setattr(api, "create_album", lambda *args: {})
    with pytest.raises(RuntimeError, match="did not contain an id"):
        migration.run_migration(api, picasa_tree, tmp_path / "out", apply=True)


def test_allow_name_only(picasa_tree, tmp_path):
    assets = [external_asset("asset-a", "a.jpg", "/lib/a.jpg", 999)]  # wrong size
    strict = migration.run_migration(FakeImmich(assets), picasa_tree, tmp_path / "strict")
    loose = migration.run_migration(FakeImmich(assets[:1]), picasa_tree, tmp_path / "loose", allow_name_only=True)
    assert strict["summary"]["matchedAssets"] == 0
    # Two local a.jpg files, one candidate each -> both low-confidence matches.
    assert loose["summary"]["lowConfidenceMatches"] == 2


# --------------------------------------------------------------------------- #
# Command line
# --------------------------------------------------------------------------- #


def test_main_scan_only(picasa_tree, tmp_path):
    assert cli.main([str(picasa_tree), "--scan-only", "--output", str(tmp_path / "out")]) == 0
    assert (only_run_dir(tmp_path / "out", "scan") / "album_file_list.txt").is_file()


def test_main_never_overwrites_earlier_runs(picasa_tree, tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "ImmichAPI", lambda *args, **kwargs: FakeImmich(inventory()))
    common = [str(picasa_tree), "--output", str(tmp_path / "out"), "--url", "http://x", "--api-key", "k"]
    for extra in ([], ["--apply"], [], ["--apply"]):
        assert cli.main(common + extra) == 0
    runs = sorted(p.name for p in (tmp_path / "out").iterdir())
    assert len(runs) == 4
    assert sum(name.endswith("-apply") or "-apply-" in name for name in runs) == 2
    for run in runs:
        report = load(tmp_path / "out" / run / "migration_report.json")
        assert report["mode"] == ("apply" if "-apply" in run else "dry-run")
        assert (tmp_path / "out" / run / "original_ini" / "2020" / ".picasa.ini").is_file()


def test_main_argument_errors_create_no_run_folder(picasa_tree, tmp_path):
    with pytest.raises(SystemExit):
        cli.main([str(picasa_tree), "--output", str(tmp_path / "out")])
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["--scan-only", "--apply"], "--apply cannot be combined"),
        ([], "Set --url and --api-key"),
        (["--url", "http://x", "--api-key", "k", "--page-size", "0"], "--page-size must be positive"),
        (["--env-file", "missing.env"], "--env-file not found"),
    ],
)
def test_main_argument_errors(picasa_tree, tmp_path, capsys, args, message):
    with pytest.raises(SystemExit) as exc:
        cli.main([str(picasa_tree), "--output", str(tmp_path / "out"), *args])
    assert exc.value.code == 2
    assert message in capsys.readouterr().err


def test_main_missing_source(tmp_path, capsys):
    with pytest.raises(SystemExit):
        cli.main([str(tmp_path / "nope"), "--scan-only"])
    assert "Source directory does not exist" in capsys.readouterr().err


def test_main_migration_uses_env_file(picasa_tree, tmp_path, monkeypatch):
    env_file = tmp_path / "test.env"
    env_file.write_text("IMMICH_URL=http://from-file\nIMMICH_API_KEY=key\n", encoding="utf-8")
    created: dict[str, object] = {}

    def fake_api(url, api_key, timeout, verify_tls, debug=False):
        created.update(url=url, api_key=api_key, verify_tls=verify_tls, debug=debug)
        return FakeImmich(inventory())

    monkeypatch.setattr(cli, "ImmichAPI", fake_api)
    code = cli.main(
        [
            str(picasa_tree),
            "--output",
            str(tmp_path / "out"),
            "--env-file",
            str(env_file),
            "--no-verify-tls",
            "--debug-api",
        ]
    )
    assert code == 0
    assert created == {"url": "http://from-file", "api_key": "key", "verify_tls": False, "debug": True}
    assert load(only_run_dir(tmp_path / "out", "dryrun") / "migration_report.json")["mode"] == "dry-run"
