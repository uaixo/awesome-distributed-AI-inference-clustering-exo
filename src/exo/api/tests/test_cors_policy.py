"""Tests for which browser origins may call the API cross-origin."""

import importlib
import os
from unittest import mock

import pytest
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

import exo.shared.constants as constants
from exo.api.main import API


def setup_cors(origins: tuple[str, ...]) -> FastAPI:
    """Run the real _setup_cors against a bare app with `origins` configured."""
    app = FastAPI()
    api = object.__new__(API)
    api.app = app
    with mock.patch("exo.api.main.EXO_API_ALLOWED_ORIGINS", origins):
        api._setup_cors()  # pyright: ignore[reportPrivateUsage]
    return app


def cors_options(app: FastAPI) -> dict[str, object] | None:
    """Return the CORS middleware's keyword arguments, or None if it is absent."""
    for middleware in app.user_middleware:
        if middleware.cls is CORSMiddleware:
            return dict(middleware.kwargs)
    return None


def test_no_cors_middleware_by_default() -> None:
    """The dashboard is served from this same origin, so it needs no CORS header."""
    assert cors_options(setup_cors(())) is None


def test_listed_origins_are_allowed_without_credentials() -> None:
    """`*` plus allow_credentials is what let any visited page read the event log."""
    options = cors_options(setup_cors(("https://ui.example",)))

    assert options is not None
    assert options["allow_origins"] == ["https://ui.example"]
    assert options["allow_credentials"] is False


def test_a_wildcard_origin_is_refused_at_startup() -> None:
    """Misconfiguration has to fail loudly rather than reopen the hole."""
    env = {**os.environ, "EXO_API_ALLOWED_ORIGINS": "https://ui.example,*"}
    with (
        mock.patch.dict(os.environ, env, clear=True),
        pytest.raises(ValueError, match="must list explicit origins"),
    ):
        _ = importlib.reload(constants)

    _ = importlib.reload(constants)
