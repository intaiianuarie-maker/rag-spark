"""The API and worker share a safe persisted-settings admission boundary."""

from __future__ import annotations

import traceback

import pytest
from connectors.config_validation import validate_settings


@pytest.mark.parametrize("kind,settings", [
    ("git", {"repo": "https://user:private-sentinel@example.com/repo.git"}),
    ("web", {"urls": ["https://user:private-sentinel@example.com/page"]}),
    ("web", {"urls": ["https://example.com"],
             "allowed_origins": ["https://private-sentinel@example.com"]}),
    ("s3", {"bucket": "b", "endpoint_ref": "private-sentinel"}),
    ("s3", {"bucket": "b", "secret": "private-sentinel"}),
    ("s3", {"bucket": "b", "private-sentinel": "unknown key"}),
])
def test_rejected_settings_do_not_leak_even_through_logged_tracebacks(kind, settings):
    with pytest.raises(ValueError) as caught:
        validate_settings(kind, settings)
    assert "private-sentinel" not in "".join(traceback.format_exception(caught.value))


@pytest.mark.parametrize("kind,key", [("local_upload", "path"), ("git", "repo")])
def test_local_paths_are_canonical_and_cannot_escape(kind, key, tmp_path, monkeypatch):
    root, outside = tmp_path / "root", tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "nested").mkdir()
    (root / "escape").symlink_to(outside, target_is_directory=True)
    monkeypatch.setenv("CONNECTOR_LOCAL_ROOTS", str(root))
    assert validate_settings(kind, {key: str(root / "nested")})[key] == str(root / "nested")
    for path in (root / ".." / "outside", root / "escape"):
        with pytest.raises(ValueError, match="approved root"):
            validate_settings(kind, {key: str(path)})
    monkeypatch.delenv("CONNECTOR_LOCAL_ROOTS")
    with pytest.raises(ValueError, match="no approved local roots"):
        validate_settings(kind, {key: str(root)})


def test_git_file_url_is_decoded_before_containment(tmp_path, monkeypatch):
    root, outside = tmp_path / "approved", tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    monkeypatch.setenv("CONNECTOR_LOCAL_ROOTS", str(root))
    assert validate_settings("git", {"repo": root.as_uri()})["repo"] == str(root)
    escaped = root.as_uri() + "/%2e%2e/outside"
    with pytest.raises(ValueError, match="approved root"):
        validate_settings("git", {"repo": escaped})


def test_web_none_defaults_never_override_ms_delay():
    settings = validate_settings("web", {"urls": ["https://example.com"],
                                         "politeness_delay_ms": 250})
    assert "politeness_delay" not in settings
    assert settings["politeness_delay_ms"] == 250


def test_git_ssh_username_is_a_transport_user_not_a_password():
    assert validate_settings("git", {"repo": "ssh://git@example.com/project.git"})["repo"] == (
        "ssh://git@example.com/project.git"
    )


@pytest.mark.parametrize("settings", [None, [], [["bucket", "b"]], "bucket=b"])
def test_non_object_persisted_settings_fail_closed(settings):
    with pytest.raises(ValueError):
        validate_settings("s3", settings)
