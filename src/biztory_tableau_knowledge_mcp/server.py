"""FastMCP server exposing the Tableau Knowledge Graph API (``/api/v1/knowledge``).

Twelve tools: nine reads (connection + graphs, semantic node search, node lookup, edges,
lineage, impact, sources, semantic contexts, graph health) and three writes that only touch
semantic contexts (create, update, delete). Read tools accept a node id OR a plain-language
description and say how it was resolved; write tools take exact ids only.
Works against a Tableau Cloud site with Tableau Knowledge (see config.py for env vars).
"""

from __future__ import annotations

import logging
import os
import sys
from collections import Counter
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from .client import TableauClient, TableauError, segment
from .config import GRAPH_ID_PATTERN, Settings

# stdio transport: logs MUST go to stderr; stdout carries the JSON-RPC protocol.
# TABLEAU_LOG_LEVEL=DEBUG logs every HTTP call (method + URL; never headers, bodies or secrets).
_LOG_LEVEL = os.environ.get("TABLEAU_LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=_LOG_LEVEL if _LOG_LEVEL in ("DEBUG", "INFO", "WARNING", "ERROR") else "INFO",
    stream=sys.stderr,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)

mcp = FastMCP("biztory-tableau-knowledge")

_client: TableauClient | None = None

# MCP tool annotations, so clients can tell reads from writes (and ask before a write) without
# reading the code. openWorldHint: every tool talks to your Tableau site.
_READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=True)
_CREATE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True)
_REPLACE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=True)

# Statement length the API enforces (as a 400); checked here first for a clearer message.
_STATEMENT_MIN, _STATEMENT_MAX = 5, 1000


def client() -> TableauClient:
    """Build the Tableau client once (lazily, so listing tools never needs credentials)."""
    global _client
    if _client is None:
        _client = TableauClient(Settings.from_env())
    return _client


def _graph(graph_id: str | None) -> dict[str, Any]:
    """The `graph_id` query parameter: the argument, else TABLEAU_KNOWLEDGE_GRAPH_ID, else omitted."""
    gid = (graph_id or "").strip() or client().s.graph_id
    if gid and not GRAPH_ID_PATTERN.match(gid):
        raise TableauError(
            "graph_id must be 1-128 letters, digits, '.', '_' or '-'. Call server_info to list graphs."
        )
    return {"graph_id": gid}


def _required(value: str | None, what: str) -> str:
    value = (value or "").strip()
    if not value:
        raise TableauError(f"Provide {what}.")
    return value


def _get_node(node_id: str, graph_id: str | None, include_children: bool) -> dict[str, Any]:
    q = {**_graph(graph_id), "include_children": include_children}
    result: dict[str, Any] = client().knowledge("GET", f"/nodes/{segment(node_id)}", query=q)
    return result


def _resolve(
    node: str, graph_id: str | None, include_children: bool = False
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Turn a node id OR a plain-language description into a node.

    An exact id wins. Otherwise the best semantic-search match is used, and the returned
    resolution (match score + runners-up) lets the model check it picked the right node.
    """
    node = _required(node, "a node id or a description of the node")
    try:
        return _get_node(node, graph_id, include_children), None
    except TableauError as e:
        if e.code != "not_found":
            raise
    found = client().knowledge(
        "POST", "/nodes/search", query=_graph(graph_id), body={"query": node, "limit": 4}
    )
    matches = (found or {}).get("matches") or []
    if not matches:
        raise TableauError(f"No node has id {node!r} and semantic search found nothing like it.")
    brief = [{k: m.get(k) for k in ("id", "name", "type", "score")} for m in matches]
    top = matches[0]
    found = _get_node(str(top["id"]), graph_id, include_children) if include_children else top
    return found, {
        "resolved_from": node,
        "match": brief[0],
        "alternatives": brief[1:],
        "note": "Not an exact id: resolved by semantic search. If this is the wrong node, call again with an alternative's id.",
    }


def _anchor(node: dict[str, Any], resolution: dict[str, Any] | None) -> dict[str, Any]:
    out: dict[str, Any] = {"node": {k: node.get(k) for k in ("id", "name", "type")}}
    if resolution:
        out["resolution"] = resolution
    return out


def _statements(statements: list[str] | None) -> list[dict[str, str]]:
    if isinstance(statements, str):
        statements = [statements]
    cleaned = [s.strip() for s in statements or [] if s and s.strip()]
    if not cleaned:
        raise TableauError("Provide at least one statement.")
    for s in cleaned:
        if not _STATEMENT_MIN <= len(s) <= _STATEMENT_MAX:
            raise TableauError(
                f"Each statement must be {_STATEMENT_MIN}-{_STATEMENT_MAX} characters; got {len(s)}: {s[:60]!r}"
            )
    return [{"statement": s} for s in cleaned]


# ---------------------------------------------------------------- reads


@mcp.tool(annotations=_READ)
def server_info() -> dict[str, Any]:
    """Report the connected Tableau site and its knowledge graphs: server URL, site, product
    version, auth method, whether Tableau Knowledge is available, and every graph (id, name,
    status, is_primary). Good first call: it confirms the connection and gives the graph ids the
    other tools accept as `graph_id` (omit `graph_id` to use the primary graph)."""
    return client().server_info()


@mcp.tool(annotations=_READ)
def search_nodes(
    query: str,
    node_type: str | None = None,
    scope_id: str | None = None,
    limit: int = 10,
    threshold: float | None = None,
    graph_id: str | None = None,
) -> dict[str, Any]:
    """Find graph nodes by MEANING (semantic search), not exact name: "revenue" can match a
    field named "Net Sales". Returns ranked matches with a 0-1 `score`; when a semantic statement
    matched, it is shown under the node's `semantic_statements`.

    - `node_type` narrows to one type: PDS (published data source), WORKBOOK, SHEET, DASHBOARD,
      EMBEDDED_DATASOURCE, TABLE, FIELD, WAREHOUSE_TABLE, WAREHOUSE_COLUMN, CONNECTION, SCHEMA,
      SITE, SEMANTIC_CONTEXT, SEMANTIC_CONTEXT_EXTERNAL.
    - `scope_id` limits the search to one data source's subtree (a PDS node id plus its tables
      and fields).
    - `limit` 1-100 (default 10); `threshold` drops matches scoring below it (0-1).

    Use the returned `id`s with get_node, list_edges, lineage and impact_analysis.
    """
    body: dict[str, Any] = {"query": _required(query, "a search query"), "limit": limit}
    if node_type:
        body["node_type"] = node_type.strip().upper()
    if scope_id:
        body["scope_id"] = scope_id.strip()
    if threshold is not None:
        body["threshold"] = threshold
    result: dict[str, Any] = client().knowledge("POST", "/nodes/search", query=_graph(graph_id), body=body)
    return result


@mcp.tool(annotations=_READ)
def get_node(node: str, include_children: bool = True, graph_id: str | None = None) -> dict[str, Any]:
    """Get one node with its properties, its semantic statements (the business context attached
    to it), and the child nodes it points to (`connected_nodes`, each with the edge type).

    `node` is a node id, or a plain-language description that is resolved by semantic search
    (the response then includes `resolution` with the match score and alternatives). Set
    `include_children` false to skip `connected_nodes` on large containers.
    """
    found, resolution = _resolve(node, graph_id, include_children)
    return {**found, "resolution": resolution} if resolution else found


@mcp.tool(annotations=_READ)
def list_edges(
    node: str, edge_type: str | None = None, direction: str | None = None, graph_id: str | None = None
) -> dict[str, Any]:
    """List the edges around one node, each with its `direction` (outgoing/incoming) and the node
    on the other end. One hop only; use lineage for the wider neighborhood.

    `node` is a node id or a plain-language description. `edge_type` narrows to one of: CONTAINS,
    HAS, JOINS, DEPENDS_ON, LINEAGE, SEMANTIC_EQUIV, DESCRIBES. `direction` is "outgoing" or
    "incoming"; omit for both.
    """
    found, resolution = _resolve(node, graph_id)
    body: dict[str, Any] = {"node_id": found["id"]}
    if edge_type:
        body["edge_type"] = edge_type.strip().upper()
    if direction:
        body["direction"] = direction.strip().lower()
    result: dict[str, Any] = client().knowledge("POST", "/edges/search", query=_graph(graph_id), body=body)
    return {**result, "resolution": resolution} if resolution else result


@mcp.tool(annotations=_READ)
def lineage(node: str, graph_id: str | None = None) -> dict[str, Any]:
    """The lineage neighborhood of a node: every node reachable within 10 steps, upstream AND
    downstream, along lineage/dependency edges (LINEAGE, DEPENDS_ON), e.g. a data source and
    the workbooks built on it. Answers "where does this come from?" and "what uses it?".

    Containment (a table HAS a field, a workbook CONTAINS a sheet) is not followed, so a single
    field often returns only itself; use list_edges or get_node to see what contains it.

    `node` is a node id or a plain-language description. Returns the anchor node, `nodes`,
    `edges` (source_id -> target_id, with type), and counts by node and edge type. For "what
    breaks if this changes?", impact_analysis is more direct.
    """
    found, resolution = _resolve(node, graph_id)
    sub = (
        client().knowledge("GET", f"/nodes/{segment(str(found['id']))}/lineage", query=_graph(graph_id)) or {}
    )
    nodes, edges = sub.get("nodes") or [], sub.get("edges") or []
    return {
        **_anchor(found, resolution),
        "counts": {
            "nodes": len(nodes),
            "edges": len(edges),
            "nodes_by_type": dict(Counter(str(n.get("type")) for n in nodes)),
            "edges_by_type": dict(Counter(str(e.get("type")) for e in edges)),
        },
        "nodes": nodes,
        "edges": edges,
    }


@mcp.tool(annotations=_READ)
def impact_analysis(node: str, graph_id: str | None = None) -> dict[str, Any]:
    """What breaks if this changes: every asset that depends on the node, directly OR transitively
    (up to 10 steps, following DEPENDS_ON), e.g. the calcs, sheets, dashboards and workbooks
    downstream of a column. The node itself is not included.

    `node` is a node id or a plain-language description. Returns `total`, `counts_by_type`, and
    `affected_assets` sorted by type then name.
    """
    found, resolution = _resolve(node, graph_id)
    res = (
        client().knowledge("GET", f"/nodes/{segment(str(found['id']))}/impact", query=_graph(graph_id)) or {}
    )
    assets = sorted(res.get("affected_assets") or [], key=lambda a: (str(a.get("type")), str(a.get("name"))))
    return {
        **_anchor(found, resolution),
        "total": len(assets),
        "counts_by_type": dict(Counter(str(a.get("type")) for a in assets)),
        "affected_assets": assets,
    }


@mcp.tool(annotations=_READ)
def list_sources(node_type: str | None = None, graph_id: str | None = None) -> dict[str, Any]:
    """List the published data sources and workbooks connected to the knowledge graph, i.e. what
    the graph has been built from. `node_type` "PDS" or "WORKBOOK" narrows it; omit for both."""
    body = {"node_type": node_type.strip().upper()} if node_type else {}
    sources = client().knowledge("POST", "/sources/search", query=_graph(graph_id), body=body) or []
    out: dict[str, Any] = {"count": len(sources), "sources": sources}
    if node_type and not sources:
        out["note"] = (
            "An unrecognised node_type returns an empty list rather than an error; try PDS or WORKBOOK."
        )
    return out


@mcp.tool(annotations=_READ)
def search_semantic_contexts(
    node: str | None = None, is_global: bool | None = None, graph_id: str | None = None
) -> dict[str, Any]:
    """List semantic contexts: the business statements (definitions, rules, caveats) attached to
    the graph. Includes user-managed (SEMANTIC_CONTEXT, editable) and Tableau-managed
    (SEMANTIC_CONTEXT_EXTERNAL, read-only) contexts.

    - With `node` (an id or a description): only the contexts attached to that node.
    - Without `node`: every context on the graph; `is_global` true = only graph-wide contexts,
      false = only node-attached ones, omitted = both.

    The API doesn't filter by text yet; to find statements by meaning, use search_nodes.
    """
    if node is not None:
        found, resolution = _resolve(node, graph_id)
        path = f"/nodes/{segment(str(found['id']))}/semantic-contexts/search"
        contexts = client().knowledge("POST", path, query=_graph(graph_id), body={}) or []
        return {**_anchor(found, resolution), "count": len(contexts), "contexts": contexts}
    body = {"is_global": is_global} if is_global is not None else {}
    contexts = (
        client().knowledge("POST", "/semantic-contexts/search", query=_graph(graph_id), body=body) or []
    )
    return {"count": len(contexts), "contexts": contexts}


@mcp.tool(annotations=_READ)
def graph_health(
    pds_id: str | None = None,
    severity: str | None = None,
    suggestion_type: str | None = None,
    limit: int = 20,
    graph_id: str | None = None,
) -> dict[str, Any]:
    """Graph-health report: a 0-100 `health_score`, coverage metrics (e.g. how many nodes carry
    semantic context), graph stats, and concrete improvement suggestions (each with severity,
    recommended_action and the target node ids), grouped by category and topic.

    `pds_id` scopes the report to one published data source's subtree. `severity` ("high",
    "medium", "low"), `suggestion_type` and `limit` (default 20; a busy graph has hundreds) filter
    only the suggestion lists; the score, metrics and `summary` totals always cover the full (or
    PDS-scoped) graph.
    """
    body: dict[str, Any] = {}
    if pds_id:
        body["pds_id"] = pds_id.strip()
    if severity:
        body["severity"] = severity.strip().lower()
    if suggestion_type:
        body["type"] = suggestion_type.strip()
    body["limit"] = limit
    result: dict[str, Any] = client().knowledge(
        "POST", "/suggestions/search", query=_graph(graph_id), body=body
    )
    return result


# ---------------------------------------------------------------- writes (semantic contexts only)


@mcp.tool(annotations=_CREATE)
def create_semantic_context(
    statements: list[str],
    target_node_id: str | None = None,
    is_global: bool = False,
    name: str | None = None,
    graph_id: str | None = None,
) -> dict[str, Any]:
    """WRITE: attach business context (one or more statements, 5-1000 characters each) to the
    knowledge graph, e.g. "Net Sales excludes returns and is reported in EUR."

    Set exactly ONE of: `target_node_id` (an exact node id from search_nodes/get_node; the
    context then DESCRIBES that node) or `is_global=true` (applies to the whole graph).
    CONNECTION, SITE, SCHEMA and semantic-context nodes can't be targets. `name` is optional
    (derived when omitted). Needs the Creator role. Returns the new context, with its id.
    """
    target = (target_node_id or "").strip() or None
    if bool(target) == bool(is_global):
        raise TableauError("Set exactly one of target_node_id (an exact node id) or is_global=true.")
    body: dict[str, Any] = {"statements": _statements(statements)}
    if target:
        body["target_node_id"] = target
    else:
        body["is_global"] = True
    if name and name.strip():
        body["name"] = name.strip()
    result: dict[str, Any] = client().knowledge(
        "POST", "/semantic-contexts", query=_graph(graph_id), body=body
    )
    return result


@mcp.tool(annotations=_REPLACE)
def update_semantic_context(
    ctx_id: str,
    statements: list[str] | None = None,
    name: str | None = None,
    attach_to: str | None = None,
    make_global: bool = False,
    graph_id: str | None = None,
) -> dict[str, Any]:
    """WRITE: change a user-managed semantic context (`ctx_id`, e.g. "semctx:<uuid>"). Only what
    you pass changes.

    - `statements` REPLACES the whole stored set (include every statement you want to keep).
    - `name` renames it.
    - `attach_to` (an exact node id) re-attaches it to that node; `make_global=true` detaches it
      and makes it graph-wide. Not both.

    Tableau-managed (SEMANTIC_CONTEXT_EXTERNAL) contexts can't be changed. Needs the Creator role.
    """
    ctx = _required(ctx_id, "the semantic context id (ctx_id)")
    target = (attach_to or "").strip() or None
    if target and make_global:
        raise TableauError("Use attach_to OR make_global, not both.")
    body: dict[str, Any] = {}
    if statements is not None:
        body["statements"] = _statements(statements)
    if name and name.strip():
        body["name"] = name.strip()
    if target:
        body.update(target_node_id=target, is_global=False)
    elif make_global:
        body.update(target_node_id=None, is_global=True)  # explicit null = detach
    if not body:
        raise TableauError("Nothing to update: pass statements, name, attach_to or make_global.")
    result: dict[str, Any] = client().knowledge(
        "PATCH", f"/semantic-contexts/{segment(ctx)}", query=_graph(graph_id), body=body
    )
    return result


@mcp.tool(annotations=_REPLACE)
def delete_semantic_context(ctx_id: str, graph_id: str | None = None) -> dict[str, Any]:
    """WRITE: delete a user-managed semantic context and its statements. Only semantic contexts
    created through Tableau Knowledge can be deleted; ids of other nodes are ignored, and
    Tableau-managed (SEMANTIC_CONTEXT_EXTERNAL) contexts are refused.

    Returns what was deleted (name and statements, so it can be recreated if needed). Deleting an
    id that doesn't exist succeeds without effect. Needs the Creator role.
    """
    ctx = _required(ctx_id, "the semantic context id (ctx_id)")
    try:
        before: dict[str, Any] | None = _get_node(ctx, graph_id, include_children=False)
    except TableauError as e:
        if e.code != "not_found":
            raise
        before = None
    if before and before.get("type") not in (None, "SEMANTIC_CONTEXT"):
        raise TableauError(
            f"{ctx} is a {before.get('type')} node, not a user-managed SEMANTIC_CONTEXT; nothing deleted."
        )
    client().knowledge("DELETE", f"/semantic-contexts/{segment(ctx)}", query=_graph(graph_id))
    if before is None:
        return {
            "ctx_id": ctx,
            "deleted": None,
            "note": "Delete accepted. The id wasn't readable beforehand, so it either didn't exist or its "
            "contents couldn't be shown; check with search_semantic_contexts.",
        }
    return {
        "ctx_id": ctx,
        "deleted": {k: before.get(k) for k in ("name", "properties", "semantic_statements")},
    }


def main() -> None:
    try:
        mcp.run()  # stdio transport
    finally:
        if _client is not None:
            _client.signout()


if __name__ == "__main__":
    main()
