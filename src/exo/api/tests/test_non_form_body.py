"""Tests that the Ollama handlers refuse bodies a cross-origin form could send."""

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from starlette.types import Message, Scope

from exo.api.main import read_non_form_body


def build_client() -> TestClient:
    """Expose read_non_form_body over a route so real headers reach it."""
    app = FastAPI()

    @app.post("/echo")
    async def echo(request: Request) -> dict[str, str]:
        return {"body": (await read_non_form_body(request)).decode()}

    _ = echo
    return TestClient(app, raise_server_exceptions=False)


@pytest.mark.parametrize(
    "content_type",
    [
        "application/x-www-form-urlencoded",
        "multipart/form-data; boundary=x",
        "text/plain",
        "TEXT/PLAIN; charset=utf-8",
    ],
)
def test_form_content_types_are_refused(content_type: str) -> None:
    """These three are the only types a page can POST without a preflight."""
    response = build_client().post(
        "/echo", content=b'{"model": "x"}', headers={"content-type": content_type}
    )

    assert response.status_code == 415


@pytest.mark.parametrize(
    "content_type",
    ["application/json", "application/x-ndjson", "application/octet-stream"],
)
def test_other_content_types_are_accepted(content_type: str) -> None:
    """Real Ollama clients vary, so the handlers stay permissive about JSON."""
    response = build_client().post(
        "/echo", content=b'{"model": "x"}', headers={"content-type": content_type}
    )

    assert response.status_code == 200
    assert response.json() == {"body": '{"model": "x"}'}


async def test_a_missing_content_type_is_accepted() -> None:
    """A client that sends no content type is not a browser form."""
    scope: Scope = {
        "type": "http",
        "headers": [],
        "method": "POST",
        "path": "/echo",
    }

    async def receive() -> Message:
        return {"type": "http.request", "body": b"{}", "more_body": False}

    assert await read_non_form_body(Request(scope, receive)) == b"{}"


def test_the_refusal_names_the_content_type() -> None:
    """An operator debugging a client needs to see what was rejected."""
    response = build_client().post(
        "/echo", content=b"{}", headers={"content-type": "text/plain"}
    )

    assert response.status_code == 415
    assert "text/plain" in response.json()["detail"]
