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


def test_default_env_file_is_used_when_present(monkeypatch, tmp_path):
    default = tmp_path / ".env"
    default.write_text("IMMICH_URL=http://default\n", encoding="utf-8")
    monkeypatch.setattr(settings, "DEFAULT_ENV_FILE", default)
    assert settings.resolve_credentials(None, None, None, environ={}).url == "http://default"


def test_explicit_env_file_must_exist(tmp_path):
    with pytest.raises(ValueError, match="--env-file not found"):
        settings.resolve_credentials(None, None, tmp_path / "missing.env", environ={})


def test_malformed_env_file_is_reported(tmp_path):
    path = tmp_path / "bad.env"
    path.write_text("BROKEN\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Cannot read"):
        settings.resolve_credentials(None, None, path, environ={})
