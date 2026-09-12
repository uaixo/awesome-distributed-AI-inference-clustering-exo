"""Shared-secret authentication for the node's HTTP API.

The API binds every interface so an operator can reach a node from another
machine, which is also what exposes it to every other host on the network, so
each API route requires a bearer token. The key is generated on first run and
kept in `EXO_API_KEY_FILE` with mode 0600.

This covers the HTTP API only. The zenoh control plane on the adjacent port
carries the same events and accepts the same commands with no authentication —
`rust/networking/src/lib.rs` listens on every interface and configures no access
control — so a token here closes the browser path and casual HTTP access, not a
determined host on the same network.
"""

import os
import secrets
from collections.abc import Iterable
from http import HTTPStatus
from pathlib import Path
from typing import cast

from loguru import logger
from starlette.responses import JSONResponse
from starlette.routing import BaseRoute, Match
from starlette.types import ASGIApp, Receive, Scope, Send

from exo.api.types.api import ErrorInfo, ErrorResponse
from exo.shared.constants import EXO_API_AUTH_DISABLED, EXO_API_KEY_FILE

API_KEY_BYTES = 32
"""Entropy of a generated key: 256 bits, past brute force across a network."""

MIN_API_KEY_LENGTH = 32
"""Shortest key accepted from either the environment or the key file.

`EXO_API_KEY` is also the name exo's own integrations page tells a user to export
for a *client*, historically with the placeholder value `x`. A length floor turns
that collision into a startup error instead of a one-character cluster secret,
and it turns a truncated key file into an error instead of an empty key that
would authenticate every request.
"""

PUBLIC_PATHS = frozenset({"/node_id"})
"""API routes reachable without a key.

A node probes a peer's `/node_id` to measure the network profile, which happens
before it could hold that peer's key; gating it tears down every topology edge.
"""

BEARER_SCHEME = b"bearer"
API_KEY_HEADER = b"x-api-key"
AUTHORIZATION_HEADER = b"authorization"


class ApiKeyError(Exception):
    """The configured or stored API key cannot be used."""


def resolve_api_key(path: Path = EXO_API_KEY_FILE) -> str | None:
    """Return the key every API route requires, or None when authentication is off.

    The disable flag is read first so it is an unconditional escape hatch:
    `EXO_API_KEY` doubles as a client-side variable name, so an operator may have
    one exported for an unrelated reason.

    Raises:
        ApiKeyError: a key is configured or stored but is too short to use, or the
            key file cannot be read or created.
    """
    if EXO_API_AUTH_DISABLED:
        logger.warning(
            "API authentication is disabled by EXO_API_AUTH_DISABLED; every route "
            "on this node is open to any host that can reach it"
        )
        return None

    configured = os.environ.get("EXO_API_KEY", "").strip()
    if configured:
        check_api_key_length(configured, "EXO_API_KEY")
        return configured

    return read_or_create_api_key(path)


def read_api_key_if_present(path: Path = EXO_API_KEY_FILE) -> str | None:
    """Return the key a client on this machine should send, without creating one.

    The client counterpart of `resolve_api_key`: a tool that generated its own key
    would hold one the node has never seen, so an absent file is reported as no
    key rather than filled in. `EXO_API_KEY` wins for the same reason it does on
    the node — an operator who sets it has chosen the cluster's key.
    """
    configured = os.environ.get("EXO_API_KEY", "").strip()
    if configured:
        return configured

    try:
        stored = path.read_text().strip() if path.exists() else ""
    except OSError:
        return None
    return stored or None


def api_key_headers(key: str | None) -> dict[str, str]:
    """Return the request headers that present `key`, or none when it is absent."""
    return {"Authorization": f"Bearer {key}"} if key else {}


def check_api_key_length(key: str, source: str) -> None:
    """Reject a key too short to resist guessing.

    Raises:
        ApiKeyError: `key` is shorter than MIN_API_KEY_LENGTH.
    """
    if len(key) < MIN_API_KEY_LENGTH:
        raise ApiKeyError(
            f"the API key from {source} is {len(key)} characters; at least "
            f"{MIN_API_KEY_LENGTH} are required. Unset it to have one generated, or "
            "set EXO_API_AUTH_DISABLED=true to serve the API without authentication"
        )


def read_or_create_api_key(path: Path) -> str:
    """Return the key stored at `path`, generating and persisting one if absent.

    The file is published with `os.replace` from a temporary file created 0600 in
    the same directory, so a reader never observes a partial key and the key is
    never briefly world-readable. Two nodes starting together both write a
    complete file and the later `os.replace` wins; both then read a usable key.

    Raises:
        ApiKeyError: the stored key is too short, or the file cannot be read or
            written.
    """
    try:
        stored = path.read_text().strip() if path.exists() else ""
    except OSError as error:
        raise ApiKeyError(f"cannot read the API key at {path}: {error}") from error

    if stored:
        check_api_key_length(stored, str(path))
        return stored

    key = secrets.token_urlsafe(API_KEY_BYTES)
    temporary = path.with_name(f"{path.name}.{os.getpid()}")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as handle:
            _ = handle.write(key)
        os.replace(temporary, path)
    except OSError as error:
        raise ApiKeyError(f"cannot create an API key at {path}: {error}") from error

    logger.info(f"generated an API key at {path}")
    return key


def public_asset_names(dashboard: Path) -> frozenset[str]:
    """Return the top-level names the dashboard mount serves without a key.

    The built dashboard is public open-source assets and the browser requests
    them with no header, so they stay reachable. Deriving the set from the build
    rather than hardcoding prefixes means a new API route is protected by
    default: anything not named here and not matching a route is refused.
    """
    try:
        return frozenset(entry.name for entry in dashboard.iterdir())
    except OSError as error:
        raise ApiKeyError(
            f"cannot list the dashboard assets at {dashboard}: {error}"
        ) from error


def first_path_segment(path: str) -> str:
    """Return the first segment of `path`, or an empty string for the root."""
    return path.lstrip("/").split("/", 1)[0]


def presented_keys(scope: Scope) -> list[bytes]:
    """Return every credential the request presents, in header order.

    Both headers are read because this API emulates OpenAI, which sends
    `Authorization: Bearer`, and Anthropic, which sends `x-api-key`. Values stay
    bytes: `secrets.compare_digest` raises on a non-ASCII `str`, so decoding here
    would turn a one-byte junk header into a 500.
    """
    credentials: list[bytes] = []
    # Starlette types the scope's values as Any; ASGI defines this one.
    headers = cast(Iterable[tuple[bytes, bytes]], scope["headers"])
    for name, value in headers:
        if name.lower() == AUTHORIZATION_HEADER:
            scheme, _, token = value.strip().partition(b" ")
            if scheme.lower() == BEARER_SCHEME:
                credentials.append(token.strip())
        elif name.lower() == API_KEY_HEADER:
            credentials.append(value.strip())
    return credentials


class BearerTokenMiddleware:
    """Refuse any API request that does not present the node's key.

    Written against ASGI directly rather than as a `BaseHTTPMiddleware`: most
    routes here return a `StreamingResponse`, and a pass-through that leaves
    `receive` and `send` untouched cannot affect flushing or client disconnects.

    A request is refused unless it is a CORS preflight, names a public path, or
    names a dashboard asset. Anything else is protected whether or not a route
    currently matches it, so adding a route cannot forget to protect it.
    """

    def __init__(
        self,
        app: ASGIApp,
        key: str,
        api_routes: tuple[BaseRoute, ...],
        asset_names: frozenset[str],
    ) -> None:
        self.app = app
        self._key = key.encode()
        self._api_routes = api_routes
        self._asset_names = asset_names

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await self.app(scope, receive, send)
            return
        if scope["type"] != "http":
            # exo declares no websocket routes, so an upgrade would otherwise reach
            # the static mount and raise per connection.
            return
        if self._is_authorized(scope) or not self._requires_key(scope):
            await self.app(scope, receive, send)
            return
        path = cast(str, scope["path"])
        logger.debug(f"refused an unauthenticated request to {path}")
        await self._refuse(scope, receive, send)

    def _requires_key(self, scope: Scope) -> bool:
        """Report whether this request must present the key."""
        if scope["method"] == "OPTIONS":
            # A browser never attaches credentials to a preflight.
            return False
        path = cast(str, scope["path"])
        if path in PUBLIC_PATHS:
            return False
        if any(route.matches(scope)[0] is not Match.NONE for route in self._api_routes):
            # PARTIAL counts: the path names an API route under another method, and
            # letting it through would forward it to the dashboard mount unchecked.
            return True
        return not (path == "/" or first_path_segment(path) in self._asset_names)

    def _is_authorized(self, scope: Scope) -> bool:
        """Report whether the request presents exactly one correct credential."""
        credentials = presented_keys(scope)
        if len(credentials) != 1:
            # Several credentials means an ambiguous request, not a valid one.
            return False
        return secrets.compare_digest(credentials[0], self._key)

    async def _refuse(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Send a 401 in the same error format the route handlers return."""
        body = ErrorResponse(
            error=ErrorInfo(
                message=(
                    "missing or invalid API key; send it as 'Authorization: Bearer "
                    "<key>' or 'x-api-key: <key>'"
                ),
                type=HTTPStatus.UNAUTHORIZED.phrase,
                code=HTTPStatus.UNAUTHORIZED.value,
            )
        )
        response = JSONResponse(
            body.model_dump(),
            status_code=HTTPStatus.UNAUTHORIZED.value,
            headers={"WWW-Authenticate": "Bearer"},
        )
        await response(scope, receive, send)
