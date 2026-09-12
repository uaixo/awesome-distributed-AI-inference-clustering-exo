"""Tests for how the node's API key is chosen, generated and persisted."""

import os
import stat
from pathlib import Path

import pytest

from exo.api.auth import (
    MIN_API_KEY_LENGTH,
    ApiKeyError,
    read_or_create_api_key,
    resolve_api_key,
)

KEY = "k" * MIN_API_KEY_LENGTH


@pytest.fixture(autouse=True)
def authentication_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """Undo the ambient environment: these tests own both inputs."""
    monkeypatch.setattr("exo.api.auth.EXO_API_AUTH_DISABLED", False)
    monkeypatch.delenv("EXO_API_KEY", raising=False)


def test_a_key_is_generated_on_first_run(tmp_path: Path) -> None:
    """Authentication is on out of the box, so the first run must mint its own key."""
    path = tmp_path / "api_key"

    key = resolve_api_key(path)

    assert len(key or "") >= MIN_API_KEY_LENGTH
    assert path.read_text() == key


def test_the_generated_key_survives_a_restart(tmp_path: Path) -> None:
    """A key that changed on every boot would invalidate every saved client config."""
    path = tmp_path / "api_key"

    assert resolve_api_key(path) == resolve_api_key(path)


def test_the_key_file_is_not_readable_by_other_users(tmp_path: Path) -> None:
    """The file is the whole secret; a shared machine must not expose it."""
    path = tmp_path / "api_key"
    _ = resolve_api_key(path)

    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_the_parent_directory_is_created(tmp_path: Path) -> None:
    """The cache directory may not exist yet on a first run."""
    path = tmp_path / "absent" / "api_key"

    assert resolve_api_key(path) is not None


def test_no_temporary_file_is_left_behind(tmp_path: Path) -> None:
    """The key is published with os.replace, so the temporary name must be gone."""
    path = tmp_path / "api_key"
    _ = resolve_api_key(path)

    assert [entry.name for entry in tmp_path.iterdir()] == ["api_key"]


def test_a_stored_key_is_used_as_it_is(tmp_path: Path) -> None:
    """An operator may write the file directly to share one key across a cluster."""
    path = tmp_path / "api_key"
    _ = path.write_text(f"  {KEY}\n")

    assert resolve_api_key(path) == KEY


def test_a_blank_key_file_is_replaced(tmp_path: Path) -> None:
    """An interrupted write leaves no key at all, which must not authenticate anyone."""
    path = tmp_path / "api_key"
    _ = path.write_text("   \n")

    key = resolve_api_key(path)

    assert len(key or "") >= MIN_API_KEY_LENGTH


def test_a_truncated_key_file_fails_loudly(tmp_path: Path) -> None:
    """Silently regenerating would lock out clients holding the longer original."""
    path = tmp_path / "api_key"
    _ = path.write_text("short")

    with pytest.raises(ApiKeyError, match="5 characters"):
        _ = resolve_api_key(path)


def test_an_unreadable_key_file_fails_loudly(tmp_path: Path) -> None:
    """Serving with no key because the file could not be read is the wrong recovery."""
    path = tmp_path / "api_key"
    path.mkdir()

    with pytest.raises(ApiKeyError, match="cannot read"):
        _ = resolve_api_key(path)


@pytest.mark.skipif(
    os.geteuid() == 0, reason="root ignores the directory mode, so nothing fails"
)
def test_an_unwritable_location_fails_loudly(tmp_path: Path) -> None:
    """A read-only cache directory must not degrade into an unauthenticated API."""
    directory = tmp_path / "read-only"
    directory.mkdir(mode=0o500)

    with pytest.raises(ApiKeyError, match="cannot create"):
        _ = read_or_create_api_key(directory / "api_key")


def test_the_environment_key_wins_over_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One shared EXO_API_KEY is how a multi-node cluster agrees on a single key."""
    path = tmp_path / "api_key"
    _ = path.write_text("f" * MIN_API_KEY_LENGTH)
    monkeypatch.setenv("EXO_API_KEY", KEY)

    assert resolve_api_key(path) == KEY


def test_the_environment_key_does_not_create_a_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Writing a second key to disk would make the node's own key ambiguous."""
    path = tmp_path / "api_key"
    monkeypatch.setenv("EXO_API_KEY", KEY)

    _ = resolve_api_key(path)

    assert not path.exists()


def test_a_short_environment_key_fails_loudly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """exo's own integrations page tells users to export EXO_API_KEY=x."""
    monkeypatch.setenv("EXO_API_KEY", "x")

    with pytest.raises(ApiKeyError, match="EXO_API_KEY"):
        _ = resolve_api_key(tmp_path / "api_key")


def test_disabling_authentication_returns_no_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The escape hatch for a network the operator already trusts."""
    monkeypatch.setattr("exo.api.auth.EXO_API_AUTH_DISABLED", True)

    assert resolve_api_key(tmp_path / "api_key") is None


def test_disabling_authentication_ignores_a_short_environment_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The flag is read first, so an EXO_API_KEY left over from a client cannot
    turn the escape hatch into a startup failure."""
    monkeypatch.setattr("exo.api.auth.EXO_API_AUTH_DISABLED", True)
    monkeypatch.setenv("EXO_API_KEY", "x")

    assert resolve_api_key(tmp_path / "api_key") is None
