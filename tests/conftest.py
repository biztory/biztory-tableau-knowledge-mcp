"""A tiny in-process fake of the Tableau Knowledge API, wired in at the urllib layer.

Tool tests go through the real client (URL building, id encoding, auth header, error mapping);
only the network is replaced. Response shapes follow the published OpenAPI spec.
"""

from __future__ import annotations

import io
import json
import urllib.error
import urllib.parse
from collections.abc import Callable
from typing import Any

import pytest

from biztory_tableau_knowledge_mcp import client as client_mod
from biztory_tableau_knowledge_mcp import server

NODES = {
    "fld:net-sales": {"id": "fld:net-sales", "type": "FIELD", "name": "Net Sales", "properties": {}},
    "wb:exec": {"id": "wb:exec", "type": "WORKBOOK", "name": "Executive Overview", "properties": {}},
    "semctx:1": {
        "id": "semctx:1",
        "type": "SEMANTIC_CONTEXT",
        "name": "Net Sales definition",
        "properties": {"statements": [{"id": "s1", "statement": "Net Sales excludes returns."}]},
    },
    "semctx:ext": {
        "id": "semctx:ext",
        "type": "SEMANTIC_CONTEXT_EXTERNAL",
        "name": "Best practice",
        "properties": {},
    },
}


class Resp:
    def __init__(self, body: Any, status: int = 200):
        self._b = b"" if body is None else json.dumps(body).encode()
        self.status = status

    def read(self) -> bytes:
        return self._b

    def __enter__(self) -> Resp:
        return self

    def __exit__(self, *a: object) -> bool:
        return False


def http_error(url: str, code: int, body: Any) -> urllib.error.HTTPError:
    raw = body if isinstance(body, bytes) else json.dumps(body).encode()
    return urllib.error.HTTPError(url, code, "error", {}, io.BytesIO(raw))  # type: ignore[arg-type]


def kerr(code: str, message: str = "nope") -> dict[str, str]:
    return {"code": code, "message": message, "trace_id": "t-1", "detail": message}


class FakeKnowledge:
    """Records every request; answers from NODES unless a test overrides a route."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.routes: dict[tuple[str, str], Callable[[dict[str, Any]], Any]] = {}

    def route(self, method: str, path: str, fn: Callable[[dict[str, Any]], Any]) -> None:
        self.routes[(method, path)] = fn

    def __call__(self, req: Any) -> Resp:
        url = urllib.parse.urlsplit(req.full_url)
        path = url.path  # still percent-encoded, exactly as sent
        call = {
            "method": req.get_method(),
            "path": path,
            "query": dict(urllib.parse.parse_qsl(url.query)),
            "body": json.loads(req.data) if req.data else None,
            "auth": req.get_header("X-tableau-auth"),
        }
        if path.endswith("/serverinfo"):
            return Resp({"serverInfo": {"productVersion": {"value": "2026.3.2"}, "restApiVersion": "3.30"}})
        self.calls.append(call)
        prefix = "/api/v1/knowledge"
        assert path.startswith(prefix), path
        sub = path[len(prefix) :]
        fn = self.routes.get((call["method"], sub))
        if fn is not None:
            out = fn(call)
            if isinstance(out, urllib.error.HTTPError):
                raise out
            return out if isinstance(out, Resp) else Resp(out)
        return self._default(call, sub, req.full_url)

    def _default(self, call: dict[str, Any], sub: str, url: str) -> Resp:
        parts = [urllib.parse.unquote(p) for p in sub.strip("/").split("/")]
        if parts[0] == "nodes" and len(parts) == 2 and call["method"] == "GET":
            node = NODES.get(parts[1])
            if node is None:
                raise http_error(url, 404, kerr("not_found", "Node not found"))
            children = [
                {"id": "wb:exec", "name": "Executive Overview", "type": "WORKBOOK", "edge_type": "DEPENDS_ON"}
            ]
            want = call["query"].get("include_children", "true") == "true"
            return Resp({**node, "semantic_statements": [], "connected_nodes": children if want else []})
        if parts[:2] == ["nodes", "search"]:
            q = call["body"]["query"].lower()
            hits = [n for n in NODES.values() if any(w in n["name"].lower() for w in q.split())]
            return Resp(
                {
                    "matches": [
                        {**n, "score": 0.9 - i / 10, "semantic_statements": []} for i, n in enumerate(hits)
                    ]
                }
            )
        raise AssertionError(f"unrouted {call['method']} {sub}")


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch) -> FakeKnowledge:
    """A fake Knowledge API behind a fresh server client authenticated with a preset token."""
    fake = FakeKnowledge()
    monkeypatch.setattr(client_mod._OPENER, "open", lambda req, timeout=None: fake(req))
    for var in (
        "TABLEAU_PAT_NAME",
        "TABLEAU_PAT_SECRET",
        "TABLEAU_KNOWLEDGE_GRAPH_ID",
        "TABLEAU_API_VERSION",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("TABLEAU_SERVER", "https://h.com")
    monkeypatch.setenv("TABLEAU_AUTH_TOKEN", "TOK")
    monkeypatch.setattr(server, "_client", None)
    return fake
