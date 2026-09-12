"""Tests that the download DELETE route refuses traversal before the handler runs.

The requests go through ASGI rather than `TestClient` for the unencoded paths:
httpx collapses `..` and `.` in a URL before sending, while hypercorn only
unquotes the path and neither it nor Starlette removes dot segments, so only a
raw scope reproduces what the server actually receives.
"""

from typing import cast

import anyio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.types import Message, Scope

from exo.shared.types.common import ModelId, NodeId


def build_app(reached: list[ModelId]) -> FastAPI:
    """Mount the real route declaration over a handler that only records its argument."""
    app = FastAPI()

    async def delete_download(node_id: NodeId, model_id: ModelId) -> dict[str, str]:
        reached.append(model_id)
        return {"model_id": model_id}

    _ = app.delete("/download/{node_id}/{model_id:path}")(delete_download)
    return app


def delete_raw_path(app: FastAPI, path: str) -> int:
    """Send one DELETE through ASGI with `path` verbatim and return the status code."""
    statuses: list[int] = []

    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: Message) -> None:
        if message["type"] == "http.response.start":
            statuses.append(cast(int, message["status"]))

    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "DELETE",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": [(b"host", b"testserver")],
        "client": ("127.0.0.1", 1234),
        "server": ("testserver", 80),
        "scheme": "http",
        "root_path": "",
    }

    async def run() -> None:
        await app(scope, receive, send)

    anyio.run(run)
    return statuses[0]


def test_parent_directory_is_refused_and_never_reaches_the_handler() -> None:
    """`..` reached delete_model as the models directory's parent, emptying ~/.exo."""
    reached: list[ModelId] = []
    status = delete_raw_path(build_app(reached), "/download/n1/..")

    assert 400 <= status < 500
    assert reached == []


def test_current_directory_is_refused() -> None:
    """`.` emptied the models directory itself."""
    reached: list[ModelId] = []
    status = delete_raw_path(build_app(reached), "/download/n1/.")

    assert 400 <= status < 500
    assert reached == []


def test_nested_parent_directories_are_refused() -> None:
    """The `:path` converter keeps every segment, so a longer climb arrives intact."""
    reached: list[ModelId] = []
    status = delete_raw_path(build_app(reached), "/download/n1/../..")

    assert 400 <= status < 500
    assert reached == []


def test_percent_encoded_parent_directory_is_refused() -> None:
    """An ordinary HTTP client reaches the same vector by encoding the dots."""
    reached: list[ModelId] = []
    response = TestClient(build_app(reached)).delete("/download/n1/%2e%2e")

    assert 400 <= response.status_code < 500
    assert reached == []


def test_a_real_model_id_still_reaches_the_handler() -> None:
    """The owner separator has to survive the `:path` converter."""
    reached: list[ModelId] = []
    response = TestClient(build_app(reached)).delete(
        "/download/n1/mlx-community/Qwen3-30B-A3B-4bit"
    )

    assert response.status_code == 200
    assert reached == [ModelId("mlx-community/Qwen3-30B-A3B-4bit")]
