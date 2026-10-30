"""Every tool, end to end through the real client against the fake Knowledge API."""

import pytest
from conftest import NODES, Resp, http_error, kerr

from biztory_tableau_knowledge_mcp import server
from biztory_tableau_knowledge_mcp.client import TableauError

# ---------------------------------------------------------------- node resolution


def test_exact_id_is_used_without_search(api):
    out = server.get_node("fld:net-sales")
    assert out["name"] == "Net Sales"
    assert "resolution" not in out
    assert [c["method"] for c in api.calls] == ["GET"]  # one call: no search, no refetch
    assert api.calls[0]["path"] == "/api/v1/knowledge/nodes/fld%3Anet-sales"
    assert api.calls[0]["query"]["include_children"] == "true"
    assert api.calls[0]["auth"] == "TOK"


def test_description_resolves_by_semantic_search_and_says_so(api):
    out = server.get_node("net sales measure", include_children=False)
    assert out["id"] == "fld:net-sales"
    res = out["resolution"]
    assert res["resolved_from"] == "net sales measure"
    assert res["match"]["id"] == "fld:net-sales"
    assert {"id", "name", "type", "score"} <= set(res["match"])
    assert "alternatives" in res and "note" in res


def test_resolution_with_children_refetches_the_match(api):
    out = server.get_node("net sales")
    assert out["connected_nodes"]  # full node, not the bare search match
    assert [c["method"] for c in api.calls] == ["GET", "POST", "GET"]


def test_unknown_description_is_a_clear_error(api):
    with pytest.raises(TableauError, match="semantic search found nothing"):
        server.get_node("zzzz qqqq")


def test_non_not_found_errors_are_not_masked_by_search(api):
    api.route("GET", "/nodes/fld%3Anet-sales", lambda c: http_error("u", 403, kerr("site_not_entitled")))
    with pytest.raises(TableauError, match="site_not_entitled") as e:
        server.get_node("fld:net-sales")
    assert "Tableau Cloud Manager" in str(e.value)
    assert len(api.calls) == 1


def test_blank_node_is_rejected(api):
    with pytest.raises(TableauError):
        server.lineage("   ")
    assert api.calls == []


def test_id_with_slash_stays_one_path_segment(api):
    with pytest.raises(TableauError):
        server.impact_analysis("../graphs?x=1")
    assert api.calls[0]["path"] == "/api/v1/knowledge/nodes/..%2Fgraphs%3Fx%3D1"


# ---------------------------------------------------------------- graph_id


def test_graph_id_argument_is_sent(api):
    server.get_node("fld:net-sales", graph_id="g-2")
    assert api.calls[0]["query"]["graph_id"] == "g-2"


def test_graph_id_defaults_from_env(api, monkeypatch):
    monkeypatch.setenv("TABLEAU_KNOWLEDGE_GRAPH_ID", "g-env")
    server.get_node("fld:net-sales")
    assert api.calls[0]["query"]["graph_id"] == "g-env"


def test_no_graph_id_means_primary_graph(api):
    server.get_node("fld:net-sales")
    assert "graph_id" not in api.calls[0]["query"]


def test_invalid_graph_id_never_leaves_the_process(api):
    with pytest.raises(TableauError, match="graph_id"):
        server.get_node("fld:net-sales", graph_id="g&x=1")
    assert api.calls == []


# ---------------------------------------------------------------- reads


def test_search_nodes_normalises_filters(api):
    api.route("POST", "/nodes/search", lambda c: {"matches": []})
    server.search_nodes("revenue", node_type="field", scope_id=" pds:1 ", limit=5, threshold=0.4)
    assert api.calls[0]["body"] == {
        "query": "revenue",
        "limit": 5,
        "node_type": "FIELD",
        "scope_id": "pds:1",
        "threshold": 0.4,
    }


def test_search_nodes_requires_query(api):
    with pytest.raises(TableauError):
        server.search_nodes(" ")


def test_list_edges_uses_resolved_id(api):
    api.route(
        "POST", "/edges/search", lambda c: {"node_id": c["body"]["node_id"], "name": "Net Sales", "edges": []}
    )
    out = server.list_edges("net sales", edge_type="lineage", direction="Outgoing")
    body = api.calls[-1]["body"]
    assert body == {"node_id": "fld:net-sales", "edge_type": "LINEAGE", "direction": "outgoing"}
    assert out["resolution"]["match"]["id"] == "fld:net-sales"


def test_lineage_adds_counts(api):
    api.route(
        "GET",
        "/nodes/fld%3Anet-sales/lineage",
        lambda c: {
            "nodes": [NODES["fld:net-sales"], NODES["wb:exec"], {**NODES["wb:exec"], "id": "wb:2"}],
            "edges": [
                {"id": "e1", "type": "DEPENDS_ON", "source_id": "wb:exec", "target_id": "fld:net-sales"}
            ],
        },
    )
    out = server.lineage("fld:net-sales")
    assert out["node"] == {"id": "fld:net-sales", "name": "Net Sales", "type": "FIELD"}
    assert out["counts"] == {
        "nodes": 3,
        "edges": 1,
        "nodes_by_type": {"FIELD": 1, "WORKBOOK": 2},
        "edges_by_type": {"DEPENDS_ON": 1},
    }


def test_impact_analysis_groups_and_sorts(api):
    assets = [
        {"id": "d2", "type": "DASHBOARD", "name": "Zeta", "properties": {}},
        {"id": "w1", "type": "WORKBOOK", "name": "Exec", "properties": {}},
        {"id": "d1", "type": "DASHBOARD", "name": "Alpha", "properties": {}},
    ]
    api.route(
        "GET",
        "/nodes/fld%3Anet-sales/impact",
        lambda c: {"node_id": "fld:net-sales", "affected_assets": assets},
    )
    out = server.impact_analysis("fld:net-sales")
    assert out["total"] == 3
    assert out["counts_by_type"] == {"DASHBOARD": 2, "WORKBOOK": 1}
    assert [a["name"] for a in out["affected_assets"]] == ["Alpha", "Zeta", "Exec"]


def test_list_sources_flags_unknown_type(api):
    api.route("POST", "/sources/search", lambda c: [])
    out = server.list_sources("datasource")
    assert api.calls[0]["body"] == {"node_type": "DATASOURCE"}
    assert out["count"] == 0 and "note" in out


def test_list_sources_all(api):
    api.route("POST", "/sources/search", lambda c: [NODES["wb:exec"]])
    out = server.list_sources()
    assert api.calls[0]["body"] == {}
    assert out == {"count": 1, "sources": [NODES["wb:exec"]]}


def test_semantic_contexts_graph_wide_with_filter(api):
    api.route("POST", "/semantic-contexts/search", lambda c: [NODES["semctx:1"]])
    out = server.search_semantic_contexts(is_global=False)
    assert api.calls[0]["body"] == {"is_global": False}
    assert out["count"] == 1


def test_semantic_contexts_for_a_node(api):
    api.route("POST", "/nodes/fld%3Anet-sales/semantic-contexts/search", lambda c: [NODES["semctx:1"]])
    out = server.search_semantic_contexts(node="fld:net-sales")
    assert out["node"]["id"] == "fld:net-sales" and out["count"] == 1


def test_graph_health_maps_arguments(api):
    api.route("POST", "/suggestions/search", lambda c: {"health_score": 72, "suggestions": []})
    out = server.graph_health(pds_id="pds:1", severity="HIGH", suggestion_type="missing_description", limit=3)
    assert api.calls[0]["body"] == {
        "pds_id": "pds:1",
        "severity": "high",
        "type": "missing_description",
        "limit": 3,
    }
    assert out["health_score"] == 72


def test_server_info_lists_graphs(api, monkeypatch):
    graphs = [{"id": "g1", "name": "Main", "status": "active", "is_primary": True}]
    api.route("GET", "/graphs", lambda c: {"graphs": graphs})
    info = server.server_info()
    assert info["knowledge_available"] is True
    assert info["graphs"] == graphs
    assert info["auth"] == "token"


def test_server_info_reports_missing_entitlement(api):
    api.route("GET", "/graphs", lambda c: http_error("u", 403, kerr("site_not_entitled", "No Knowledge")))
    info = server.server_info()
    assert info["knowledge_available"] is False
    assert "site_not_entitled" in info["knowledge_error"]


def test_no_embedder_503_explains_the_fallback(api):
    api.route(
        "POST", "/nodes/search", lambda c: http_error("u", 503, kerr("internal", "No embedder configured"))
    )
    with pytest.raises(TableauError, match="Semantic search is unavailable"):
        server.search_nodes("revenue")


# ---------------------------------------------------------------- writes


def test_create_attached_context(api):
    api.route(
        "POST", "/semantic-contexts", lambda c: Resp({"id": "semctx:new", "type": "SEMANTIC_CONTEXT"}, 201)
    )
    out = server.create_semantic_context(
        ["  Net Sales excludes returns.  "], target_node_id="fld:net-sales", name="Def"
    )
    assert api.calls[0]["body"] == {
        "statements": [{"statement": "Net Sales excludes returns."}],
        "target_node_id": "fld:net-sales",
        "name": "Def",
    }
    assert out["id"] == "semctx:new"


def test_create_global_context(api):
    api.route("POST", "/semantic-contexts", lambda c: {"id": "semctx:g"})
    server.create_semantic_context(["Fiscal year starts in April."], is_global=True)
    assert api.calls[0]["body"] == {
        "statements": [{"statement": "Fiscal year starts in April."}],
        "is_global": True,
    }


@pytest.mark.parametrize(
    "kwargs",
    [
        {"statements": ["Valid statement"]},  # neither target nor global
        {"statements": ["Valid statement"], "target_node_id": "x", "is_global": True},  # both
        {"statements": [], "is_global": True},
        {"statements": ["  "], "is_global": True},
        {"statements": ["tiny"], "is_global": True},  # < 5 chars
        {"statements": ["x" * 1001], "is_global": True},  # > 1000 chars
    ],
)
def test_create_validation_happens_before_any_request(api, kwargs):
    with pytest.raises(TableauError):
        server.create_semantic_context(**kwargs)
    assert api.calls == []


def test_update_replaces_statements_and_renames(api):
    api.route("PATCH", "/semantic-contexts/semctx%3A1", lambda c: {"id": "semctx:1"})
    server.update_semantic_context("semctx:1", statements=["New definition text."], name="Renamed")
    assert api.calls[0]["body"] == {"statements": [{"statement": "New definition text."}], "name": "Renamed"}


def test_update_make_global_sends_explicit_null(api):
    api.route("PATCH", "/semantic-contexts/semctx%3A1", lambda c: {"id": "semctx:1"})
    server.update_semantic_context("semctx:1", make_global=True)
    assert api.calls[0]["body"] == {"target_node_id": None, "is_global": True}


def test_update_attach_to(api):
    api.route("PATCH", "/semantic-contexts/semctx%3A1", lambda c: {"id": "semctx:1"})
    server.update_semantic_context("semctx:1", attach_to="wb:exec")
    assert api.calls[0]["body"] == {"target_node_id": "wb:exec", "is_global": False}


@pytest.mark.parametrize(
    "kwargs",
    [{}, {"attach_to": "wb:exec", "make_global": True}, {"statements": []}],
)
def test_update_validation(api, kwargs):
    with pytest.raises(TableauError):
        server.update_semantic_context("semctx:1", **kwargs)
    assert api.calls == []


def test_update_external_context_conflict_is_explained(api):
    api.route("PATCH", "/semantic-contexts/semctx%3Aext", lambda c: http_error("u", 409, kerr("conflict")))
    with pytest.raises(TableauError, match="Tableau-managed"):
        server.update_semantic_context("semctx:ext", name="x")


def test_delete_returns_what_was_deleted(api):
    api.route("DELETE", "/semantic-contexts/semctx%3A1", lambda c: Resp(None, 204))
    out = server.delete_semantic_context("semctx:1")
    assert out["deleted"]["name"] == "Net Sales definition"
    assert out["deleted"]["properties"]["statements"][0]["statement"] == "Net Sales excludes returns."
    assert [c["method"] for c in api.calls] == ["GET", "DELETE"]


def test_delete_refuses_non_context_nodes_locally(api):
    with pytest.raises(TableauError, match="nothing deleted"):
        server.delete_semantic_context("wb:exec")
    with pytest.raises(TableauError, match="nothing deleted"):
        server.delete_semantic_context("semctx:ext")
    assert all(c["method"] == "GET" for c in api.calls)


def test_delete_unknown_id_is_idempotent_and_honest(api):
    api.route("DELETE", "/semantic-contexts/semctx%3Agone", lambda c: Resp(None, 204))
    out = server.delete_semantic_context("semctx:gone")
    assert out["deleted"] is None and "either didn't exist" in out["note"]


def test_annotations_tell_reads_from_writes():
    tools = {t.name: t.annotations for t in server.mcp._tool_manager.list_tools()}
    writes = {"create_semantic_context", "update_semantic_context", "delete_semantic_context"}
    for name, ann in tools.items():
        assert ann is not None and ann.openWorldHint is True, name
        assert ann.readOnlyHint is (name not in writes), name
    assert tools["create_semantic_context"].destructiveHint is False
    assert tools["update_semantic_context"].destructiveHint is True
    assert tools["delete_semantic_context"].destructiveHint is True
    assert tools["delete_semantic_context"].idempotentHint is True


def test_graph_health_caps_suggestions_by_default(api):
    api.route("POST", "/suggestions/search", lambda c: {"health_score": 51, "suggestions": []})
    server.graph_health()
    assert api.calls[0]["body"] == {"limit": 20}
