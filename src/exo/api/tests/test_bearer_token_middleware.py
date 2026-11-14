"""Tests that the API key is required everywhere except the public surface."""

from http import HTTPStatus
from pathlib import Path
from typing import cast

import pytest
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient

from exo.api.auth import CLUSTER_PATHS, BearerTokenMiddleware
from exo.api.cluster_routes import SERVING_PATH, ClusterRoutes
from exo.api.main import API
from exo.api.serving_client import ServingClient
from exo.routing.tests.fake_membership import FakeMembership
from exo.shared.types.common import NodeId
from exo.shared.types.state import State
from exo.worker.serving import SERVE_PATH

KEY = "k" * 40
ASSET_NAMES = frozenset({"_app", "index.html", "favicon.ico", "exo-logo.png"})
NODE_ID = NodeId("test-node")
LOOPBACK = ("127.0.0.1", 50000)
"""An address the cluster's own routes answer: this machine."""
STRANGER = ("203.0.113.9", 50000)
"""An address of no node in the cluster."""


def api_with_routes(app: FastAPI) -> API:
    """An API holding only what `_setup_routes` reads, so the real route table is built."""
    api = object.__new__(API)
    api.app = app
    api.node_id = NODE_ID
    api.state = State()
    api._serving_host = None  # pyright: ignore[reportPrivateUsage]
    api._serving = ServingClient(  # pyright: ignore[reportPrivateUsage]
        NODE_ID,
        api_port=52415,
        host=None,
        state=lambda: api.state,
        reachability=FakeMembership(NODE_ID),
    )
    return api


@pytest.fixture
def dashboard(tmp_path: Path) -> Path:
    """A stand-in built dashboard with the same top-level shape as the real one."""
    (tmp_path / "_app" / "immutable").mkdir(parents=True)
    (tmp_path / "_app" / "immutable" / "app.js").write_text("export {}")
    (tmp_path / "index.html").write_text("<!doctype html>")
    (tmp_path / "favicon.ico").write_bytes(b"\x00")
    return tmp_path


@pytest.fixture
def client(dashboard: Path) -> TestClient:
    """The real route table and middleware over a stand-in dashboard mount."""
    app = FastAPI()
    api_with_routes(app)._setup_routes()  # pyright: ignore[reportPrivateUsage]
    app.add_middleware(
        BearerTokenMiddleware,
        key=KEY,
        api_routes=tuple(app.routes),
        asset_names=ASSET_NAMES,
    )
    app.mount("/", StaticFiles(directory=dashboard, html=True), name="dashboard")
    return TestClient(app, raise_server_exceptions=False)


def authorized(header_name: str, value: str) -> dict[str, str]:
    return {header_name: value}


@pytest.mark.parametrize("path", ["/", "/index.html", "/_app/immutable/app.js"])
def test_dashboard_assets_are_served_without_a_key(
    client: TestClient, path: str
) -> None:
    """The browser fetches these with no header; gating them hides the key prompt itself."""
    assert client.get(path).status_code == HTTPStatus.OK


def test_node_id_is_public(client: TestClient) -> None:
    """Peers probe /node_id before they could hold this node's key."""
    assert client.get("/node_id").status_code == HTTPStatus.OK


def cluster_client(address: tuple[str, int]) -> TestClient:
    """The real route table and middleware, called from `address` with no key."""
    app = FastAPI()
    api_with_routes(app)._setup_routes()  # pyright: ignore[reportPrivateUsage]
    app.add_middleware(
        BearerTokenMiddleware,
        key=KEY,
        api_routes=tuple(app.routes),
        asset_names=ASSET_NAMES,
    )
    return TestClient(app, raise_server_exceptions=False, client=address)


def test_a_node_of_the_cluster_reaches_its_routes_without_a_key() -> None:
    """A node holds its own key, not its peers', so a key here would refuse every
    request one node serves for another."""
    member = cluster_client(LOOPBACK)

    assert member.get(SERVING_PATH).status_code == HTTPStatus.OK
    # Past the middleware and the member check, to the handler: this node runs no models
    assert member.post(SERVE_PATH, content=b"{}").status_code == HTTPStatus.NOT_FOUND


def test_the_cluster_routes_still_refuse_anyone_outside_the_cluster() -> None:
    """Their own check on the caller's address is what guards them, not the key."""
    stranger = cluster_client(STRANGER)

    for path in CLUSTER_PATHS:
        assert stranger.get(path).status_code in (
            HTTPStatus.FORBIDDEN,
            HTTPStatus.METHOD_NOT_ALLOWED,
        )
    assert stranger.get(SERVING_PATH).status_code == HTTPStatus.FORBIDDEN
    assert stranger.post(SERVE_PATH, content=b"{}").status_code == HTTPStatus.FORBIDDEN


def test_the_key_is_still_required_from_a_node_of_the_cluster_elsewhere() -> None:
    """Being a member opens the cluster's routes only, never the API."""
    assert cluster_client(LOOPBACK).get("/state").status_code == HTTPStatus.UNAUTHORIZED


def test_every_route_the_cluster_registers_is_reachable_without_a_key() -> None:
    """A route added to `ClusterRoutes` and not to `CLUSTER_PATHS` would answer 401 to
    every other node, which is requests failing across the cluster, not a visible error."""
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    ClusterRoutes(state=State, host=None).add_to(app)

    registered = {cast(str, getattr(route, "path", "")) for route in app.routes}

    assert registered == CLUSTER_PATHS


def test_an_api_route_is_refused_without_a_key(client: TestClient) -> None:
    """GET /state returned the whole cluster state to any host on the network."""
    assert client.get("/state").status_code == HTTPStatus.UNAUTHORIZED


def test_the_refusal_uses_the_api_error_format(client: TestClient) -> None:
    """A 401 that looks different from every other error is a client bug waiting to happen."""
    response = client.get("/state")

    assert response.headers["www-authenticate"] == "Bearer"
    assert response.json() == {
        "error": {
            "message": (
                "missing or invalid API key; send it as 'Authorization: Bearer "
                "<key>' or 'x-api-key: <key>'"
            ),
            "type": "Unauthorized",
            "param": None,
            "code": 401,
        }
    }


@pytest.mark.parametrize(
    ("header", "value"),
    [
        ("Authorization", f"Bearer {KEY}"),
        ("Authorization", f"bearer {KEY}"),
        ("authorization", f"BEARER {KEY}"),
        ("x-api-key", KEY),
    ],
)
def test_every_accepted_credential_form(
    client: TestClient, header: str, value: str
) -> None:
    """The scheme is case-insensitive per RFC 7235, and this API also emulates Anthropic."""
    response = client.get("/state", headers=authorized(header, value))

    assert response.status_code != HTTPStatus.UNAUTHORIZED


@pytest.mark.parametrize(
    ("header", "value"),
    [
        ("Authorization", "Bearer "),
        ("Authorization", "Bearer wrong"),
        ("Authorization", KEY),
        ("Authorization", f"Basic {KEY}"),
        ("x-api-key", ""),
        ("x-api-key", "wrong"),
    ],
)
def test_every_rejected_credential_form(
    client: TestClient, header: str, value: str
) -> None:
    """An empty credential must never match, whatever the key is."""
    response = client.get("/state", headers=authorized(header, value))

    assert response.status_code == HTTPStatus.UNAUTHORIZED


def test_a_non_utf8_credential_is_refused_not_crashed(client: TestClient) -> None:
    """secrets.compare_digest raises on a non-ASCII str, so the value stays bytes.

    Sent as bytes because an HTTP client refuses to encode such a header from text;
    a server reading the wire hands these bytes to the application unchanged.
    """
    response = client.get("/state", headers={b"authorization": b"Bearer \xff"})

    assert response.status_code == HTTPStatus.UNAUTHORIZED


def test_two_credentials_are_refused(client: TestClient) -> None:
    """An ambiguous request is not a valid one, whichever header a parser would pick."""
    response = client.get(
        "/state", headers={"Authorization": f"Bearer {KEY}", "x-api-key": "wrong"}
    )

    assert response.status_code == HTTPStatus.UNAUTHORIZED


def test_a_wrong_method_on_an_api_path_is_still_refused(client: TestClient) -> None:
    """route.matches returns PARTIAL here; treating that as 'not an API path' would
    forward the request to the dashboard mount with no key."""
    response = client.put("/state")

    assert response.status_code == HTTPStatus.UNAUTHORIZED


def test_a_preflight_is_not_refused(client: TestClient) -> None:
    """A browser never attaches credentials to a preflight."""
    response = client.options(
        "/state",
        headers={
            "Origin": "https://ui.example",
            "Access-Control-Request-Method": "GET",
        },
    )

    assert response.status_code != HTTPStatus.UNAUTHORIZED


def test_an_unknown_path_is_refused(client: TestClient) -> None:
    """Neither a known route nor a dashboard asset, so it fails closed."""
    assert client.get("/not-a-route").status_code == HTTPStatus.UNAUTHORIZED


def test_a_route_added_after_the_snapshot_is_still_protected(dashboard: Path) -> None:
    """The snapshot is an optimisation, not the rule: protection is by exclusion."""
    app = FastAPI()
    app.add_middleware(
        BearerTokenMiddleware,
        key=KEY,
        api_routes=(),
        asset_names=ASSET_NAMES,
    )

    @app.get("/added-later")
    async def added_later() -> dict[str, bool]:
        return {"reached": True}

    _ = added_later
    # After the route, so the catch-all mount stays last in the router and the
    # request reaches the handler; the middleware still holds no route snapshot.
    app.mount("/", StaticFiles(directory=dashboard, html=True), name="dashboard")
    client = TestClient(app, raise_server_exceptions=False)

    assert client.get("/added-later").status_code == HTTPStatus.UNAUTHORIZED
    assert (
        client.get("/added-later", headers={"x-api-key": KEY}).status_code
        == HTTPStatus.OK
    )


def test_cors_is_registered_outside_authentication(
    dashboard: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Starlette applies the last-added middleware outermost, so _setup_cors must
    run after _setup_auth; the reverse order strips the CORS header off every 401."""
    app = FastAPI()
    api = api_with_routes(app)
    monkeypatch.setattr("exo.api.main.resolve_api_key", lambda: KEY)
    monkeypatch.setattr("exo.api.main.dashboard_dir", lambda: dashboard)
    monkeypatch.setattr("exo.api.main.EXO_API_ALLOWED_ORIGINS", ("https://ui.example",))

    api._setup_routes()  # pyright: ignore[reportPrivateUsage]
    api._setup_auth()  # pyright: ignore[reportPrivateUsage]
    api._setup_cors()  # pyright: ignore[reportPrivateUsage]
    app.mount("/", StaticFiles(directory=dashboard, html=True), name="dashboard")

    registered = [
        cast(type, middleware.cls).__name__ for middleware in app.user_middleware
    ]
    assert registered.index(CORSMiddleware.__name__) < registered.index(
        BearerTokenMiddleware.__name__
    )

    response = TestClient(app, raise_server_exceptions=False).get(
        "/state", headers={"Origin": "https://ui.example"}
    )

    assert response.status_code == HTTPStatus.UNAUTHORIZED
    assert response.headers["access-control-allow-origin"] == "https://ui.example"
