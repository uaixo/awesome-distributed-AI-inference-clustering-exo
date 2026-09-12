# type: ignore
"""HTTP client for the exo API."""

from __future__ import annotations

import http.client
import json
import os
from collections.abc import Iterator
from typing import Any
from urllib.parse import urlencode


def resolve_api_key() -> str | None:
    """Return the key a node expects on every API route, from the environment.

    Only the environment: this package deliberately does not depend on `exo`, and
    a node's key file lives on the node's own machine, which is usually not the
    one running a benchmark. Export it with the line the node logs at startup.
    """
    return os.environ.get("EXO_API_KEY", "").strip() or None


def api_key_headers(api_key: str | None) -> dict[str, str]:
    """Return the request headers that present `api_key`, or none when absent."""
    return {"Authorization": f"Bearer {api_key}"} if api_key else {}


class ExoHttpError(RuntimeError):
    def __init__(self, status: int, reason: str, body_preview: str):
        super().__init__(f"HTTP {status} {reason}: {body_preview}")
        self.status = status


class ExoClient:
    def __init__(
        self,
        host: str,
        port: int,
        timeout_s: float = 7200.0,
        api_key: str | None = None,
    ):
        """Talk to the exo API at `host`:`port`.

        `api_key` defaults to `EXO_API_KEY`, so every caller that already built a
        client keeps working once that is exported. Pass it explicitly to target
        two clusters with different keys from one process.
        """
        self.host = host
        self.port = port
        self.timeout_s = timeout_s
        self.api_key = api_key if api_key is not None else resolve_api_key()

    def request_json(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        if not path.startswith("/"):
            path = "/" + path
        if params:
            path = path + "?" + urlencode(params)

        conn = http.client.HTTPConnection(self.host, self.port, timeout=self.timeout_s)
        try:
            payload: bytes | None = None
            hdrs: dict[str, str] = {
                "Accept": "application/json",
                **api_key_headers(self.api_key),
            }

            if body is not None:
                payload = json.dumps(body).encode("utf-8")
                hdrs["Content-Type"] = "application/json"
            if headers:
                hdrs.update(headers)

            conn.request(method.upper(), path, body=payload, headers=hdrs)
            resp = conn.getresponse()
            raw = resp.read()
            text = raw.decode("utf-8", errors="replace") if raw else ""

            if resp.status >= 400:
                raise ExoHttpError(resp.status, resp.reason, text[:300])

            if not text:
                return None
            return json.loads(text)
        finally:
            conn.close()

    def post_bench_chat_completions(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.request_json("POST", "/bench/chat/completions", body=payload)

    def stream_bench_chat_completions(self, payload: dict[str, Any]) -> Iterator[str]:
        """POST /bench/chat/completions with stream=True, yielding raw SSE lines."""
        payload = {**payload, "stream": True}
        data = json.dumps(payload).encode("utf-8")
        conn = http.client.HTTPConnection(self.host, self.port, timeout=self.timeout_s)
        try:
            conn.request(
                "POST",
                "/bench/chat/completions",
                body=data,
                headers={
                    "Content-Type": "application/json",
                    "Accept": "text/event-stream",
                    **api_key_headers(self.api_key),
                },
            )
            resp = conn.getresponse()
            if resp.status >= 400:
                raw = resp.read().decode("utf-8", errors="replace")
                raise ExoHttpError(resp.status, resp.reason, raw[:300])
            for line in resp:
                yield line.decode("utf-8", errors="replace")
        finally:
            conn.close()

    def get_state_path(self, path: str) -> Any:
        try:
            return self.request_json("GET", f"/state/{path}")
        except ExoHttpError as e:
            if e.status == 404:
                return None
            raise

    def get_instance(self, instance_id: str) -> dict[str, Any] | None:
        return self.get_state_path(f"instances/{instance_id}")

    def get_runner(self, runner_id: str) -> dict[str, Any] | None:
        return self.get_state_path(f"runners/{runner_id}")

    def get_node_downloads(self, node_id: str) -> list[dict[str, Any]] | None:
        return self.get_state_path(f"downloads/{node_id}")

    def get_node_disk(self, node_id: str) -> dict[str, Any] | None:
        return self.get_state_path(f"nodeDisk/{node_id}")

    def get_node_system(self, node_id: str) -> dict[str, Any] | None:
        return self.get_state_path(f"nodeSystem/{node_id}")

    def get_node_identities(self) -> dict[str, Any] | None:
        return self.get_state_path("nodeIdentities")

    def get_topology(self) -> dict[str, Any] | None:
        return self.get_state_path("topology")
