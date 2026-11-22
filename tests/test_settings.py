import pytest

from picasa_immich import settings


def test_read_env_file_formats(tmp_path):
    path = tmp_path / ".env"
    path.write_text(
        "# comment\n\nexport IMMICH_URL=\"http://server:2283\"\n"
        "IMMICH_API_KEY='secret'\nPLAIN = value with spaces \nEMPTY=\nURL=a=b\n",
        encoding="utf-8",
    )
    assert settings.read_env_file(path) == {
        "IMMICH_URL": "http://server:2283",
        "IMMICH_API_KEY": "secret",
        "PLAIN": "value with spaces",
        "EMPTY": "",
        "URL": "a=b",
    }


def test_read_env_file_handles_bom(tmp_path):
    path = tmp_path / ".env"
    path.write_bytes("﻿IMMICH_URL=x\n".encode("utf-8"))
    assert settings.read_env_file(path) == {"IMMICH_URL": "x"}


def test_read_env_file_missing_returns_empty(tmp_path):
    assert settings.read_env_file(tmp_path / "missing.env") == {}


@pytest.mark.parametrize("line", ["NO_EQUALS_SIGN", "=value"])
def test_read_env_file_rejects_malformed_lines(tmp_path, line):
    path = tmp_path / ".env"
    path.write_text(f"OK=1\n{line}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="line 2"):
        settings.read_env_file(path)


@pytest.fixture
def env_file(tmp_path):
    path = tmp_path / "settings.env"
    path.write_text("IMMICH_URL=http://file\nIMMICH_API_KEY=file-key\n", encoding="utf-8")
    return path


def test_credentials_from_file(env_file):
    creds = settings.resolve_credentials(None, None, env_file, environ={})
    assert (creds.url, creds.api_key, creds.env_file_used) == ("http://file", "file-key", env_file)


def test_environment_beats_file(env_file):
    creds = settings.resolve_credentials(None, None, env_file, environ={"IMMICH_URL": "http://env"})
    assert (creds.url, creds.api_key) == ("http://env", "file-key")


def test_command_line_beats_environment_and_file(env_file):
    creds = settings.resolve_credentials("http://cli", "cli-key", env_file,
                                    environ={"IMMICH_URL": "http://env", "IMMICH_API_KEY": "env-key"})
    assert (creds.url, creds.api_key) == ("http://cli", "cli-key")


def test_default_env_file_is_optional():
    creds = settings.resolve_credentials(None, None, None, environ={})
    assert creds == settings.Credentials(None, None, None)


def test_first_existing_default_env_file_is_used(monkeypatch, tmp_path):
    first, second, third = tmp_path / "a.env", tmp_path / "b.env", tmp_path / "c.env"
    second.write_text("IMMICH_URL=http://second\n", encoding="utf-8")
    third.write_text("IMMICH_URL=http://third\nIMMICH_API_KEY=third-key\n", encoding="utf-8")
    monkeypatch.setattr(settings, "default_env_files", lambda: [first, second, third])
    creds = settings.resolve_credentials(None, None, None, environ={})
    # Only one file is read: values are not merged from later files.
    assert (creds.url, creds.api_key, creds.env_file_used) == ("http://second", None, second)


def test_default_env_files_are_cwd_then_config_dir(monkeypatch, tmp_path):
    monkeypatch.undo()  # drop the conftest isolation for this test
    monkeypatch.chdir(tmp_path)
    files = settings.default_env_files()
    assert files == [tmp_path / ".env", settings.user_config_dir() / ".env"]


@pytest.mark.parametrize(("platform", "environ", "expected"), [
    ("win32", {"APPDATA": "C:/Users/me/AppData/Roaming"}, "C:/Users/me/AppData/Roaming/picasa-to-immich"),
    ("win32", {}, "/home/me/AppData/Roaming/picasa-to-immich"),
    ("darwin", {}, "/home/me/Library/Application Support/picasa-to-immich"),
    ("linux", {"XDG_CONFIG_HOME": "/xdg"}, "/xdg/picasa-to-immich"),
    ("linux", {}, "/home/me/.config/picasa-to-immich"),
])
def test_user_config_dir(platform, environ, expected):
    from pathlib import Path
    assert settings.user_config_dir(environ, platform, home=Path("/home/me")) == Path(expected)


def test_explicit_env_file_must_exist(tmp_path):
    with pytest.raises(ValueError, match="--env-file not found"):
        settings.resolve_credentials(None, None, tmp_path / "missing.env", environ={})


def test_malformed_env_file_is_reported(tmp_path):
    path = tmp_path / "bad.env"
    path.write_text("BROKEN\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Cannot read"):
        settings.resolve_credentials(None, None, path, environ={})
