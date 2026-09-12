"""Tests that dashboard assets are located on demand, not at import."""

import importlib
import os
from pathlib import Path
from unittest import mock

import pytest

import exo.shared.constants as constants


def test_importing_constants_does_not_locate_the_dashboard():
    """A checkout without `dashboard/build` must still be importable, so pytest can collect."""
    env = {k: v for k, v in os.environ.items() if k != "EXO_DASHBOARD_DIR"}
    with (
        mock.patch.dict(os.environ, env, clear=True),
        mock.patch.object(
            constants, "find_dashboard", side_effect=FileNotFoundError
        ) as find_dashboard,
    ):
        importlib.reload(constants)
        find_dashboard.assert_not_called()

    importlib.reload(constants)


def test_dashboard_dir_reports_a_missing_build():
    """The error a contributor who skipped `npm run build` needs to see, raised where it is used."""
    env = {k: v for k, v in os.environ.items() if k != "EXO_DASHBOARD_DIR"}
    with (
        mock.patch.dict(os.environ, env, clear=True),
        mock.patch.object(constants, "find_dashboard", side_effect=FileNotFoundError),
        pytest.raises(FileNotFoundError),
    ):
        _ = constants.dashboard_dir()


def test_dashboard_dir_honours_the_env_override():
    """EXO_DASHBOARD_DIR wins over the search, and an absolute value is used as given."""
    with mock.patch.dict(
        os.environ, {"EXO_DASHBOARD_DIR": "/tmp/exo-dashboard"}, clear=False
    ):
        assert Path("/tmp/exo-dashboard") == constants.dashboard_dir()
