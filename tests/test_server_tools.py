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
    writes = {
        "create_semantic_context",
        "update_semantic_context",
        "delete_semantic_context",
        "import_from_ossie",
        "add_sources",
        "remove_sources",
        "create_graph",
        "update_graph",
        "ask_tableau_agent",
        "create_semantic_context_from_document",
    }
    local = {"validate_ossie", "ossie_guide"}  # never talk to Tableau
    for name, ann in tools.items():
        assert ann is not None and ann.openWorldHint is (name not in local), name
        assert ann.readOnlyHint is (name not in writes), name
    assert tools["import_from_ossie"].destructiveHint is True
    assert tools["create_semantic_context"].destructiveHint is False
    assert tools["update_semantic_context"].destructiveHint is True
    assert tools["delete_semantic_context"].destructiveHint is True
    assert tools["delete_semantic_context"].idempotentHint is True
    assert tools["add_sources"].destructiveHint is False
    assert tools["remove_sources"].destructiveHint is True
    assert tools["create_graph"].destructiveHint is False
    assert tools["update_graph"].destructiveHint is True
    assert tools["ask_tableau_agent"].destructiveHint is True  # the agent can write statements


def test_graph_health_caps_suggestions_by_default(api):
    api.route("POST", "/suggestions/search", lambda c: {"health_score": 51, "suggestions": []})
    server.graph_health()
    assert api.calls[0]["body"] == {"limit": 20}


# ---------------------------------------------------------------- graph sources (internal API)

G = "g1"
L1 = "fe1e5ddd-ef32-4a3f-bbb7-28d2fd537522"
L2 = "74ceb97a-d290-4b70-8ece-89b89460ed1e"
SRC = [
    {"id": f"pds:c:{L1}", "name": "Sample - Superstore", "type": "PDS", "properties": {"luid": L1}},
    {"id": f"workbook:c:{L2}", "name": "Overview", "type": "WORKBOOK", "properties": {"luid": L2}},
]


def test_list_sources_adds_ingest_status_from_internal_api(api):
    api.route("POST", "/sources/search", lambda c: SRC)
    api.route(
        "GET",
        f"/ui/tableau-cloud/ingest-async/by-graph/{G}",
        lambda c: {"status": "completed", "result": {"per_asset": [{"asset_type": "pds", "ok": True}]}},
    )
    out = server.list_sources(graph_id=G)
    assert out["ingest"] == {"status": "completed", "per_asset": [{"asset_type": "pds", "ok": True}]}
    assert api.calls[1]["auth"] == "TOK"


def test_list_sources_without_ingest_status_still_works(api):
    api.route("POST", "/sources/search", lambda c: SRC)  # by-graph unrouted -> 404
    out = server.list_sources(graph_id=G)
    assert out["count"] == 2 and "ingest" not in out


def test_list_sources_site_search_pages_filters_and_flags_in_graph(api):
    api.route("POST", "/sources/search", lambda c: SRC)
    pages = {
        None: {
            "datasources": [{"luid": L1, "name": "Sample - Superstore"}, {"luid": "x", "name": "HR"}],
            "has_next_page": True,
            "next_page_token": "2",
        },
        "2": {"datasources": [{"luid": "y", "name": "Global SUPERSTORE"}], "has_next_page": False},
    }
    api.route("GET", "/ui/tableau-cloud/datasources", lambda c: pages[c["query"].get("page_token")])
    out = server.list_sources(node_type="PDS", graph_id=G, site_search="superstore")
    assert [(d["name"], d["in_graph"]) for d in out["datasources"]] == [
        ("Sample - Superstore", True),
        ("Global SUPERSTORE", False),
    ]
    assert "workbooks" not in out and "note" not in out


def test_list_sources_site_search_says_when_cut_off(api, monkeypatch):
    monkeypatch.setattr(server, "_SITE_MAX_PAGES", 2)
    api.route("POST", "/sources/search", lambda c: [])
    api.route(
        "GET",
        "/ui/tableau-cloud/workbooks",
        lambda c: {"workbooks": [], "has_next_page": True, "next_page_token": "n"},
    )
    out = server.list_sources(node_type="WORKBOOK", site_search="x")
    assert out["workbooks"] == [] and "first 200 workbooks" in out["note"]


def test_add_sources_sends_only_non_empty_targets(api):
    api.route(
        "POST",
        "/ui/tableau-cloud/ingest-async",
        lambda c: Resp({"status": "running", "workflow_id": "w"}, 202),
    )
    out = server.add_sources(pds_luids=[L1.upper(), L1], graph_id=G)
    call = api.calls[0]
    assert call["query"] == {"graph_id": G}
    assert call["body"] == {"targets": [{"asset_type": "pds", "luids": [L1]}]}
    assert out["status"] == "running" and out["requested"] == {"pds": [L1]}


@pytest.mark.parametrize(
    "kwargs",
    [
        {"graph_id": G},  # nothing: Tableau would read an empty list as everything
        {"pds_luids": [], "workbook_luids": [" "], "graph_id": G},
        {"pds_luids": ["Sample - Superstore"], "graph_id": G},  # names are not ids
        {"pds_luids": [f"pds:c:{L1}"], "graph_id": G},
        {"pds_luids": [L1]},  # no graph: never the primary graph by default
    ],
)
def test_source_writes_validate_before_any_request(api, kwargs):
    for tool in (server.add_sources, server.remove_sources):
        with pytest.raises(TableauError):
            tool(**kwargs)
    assert api.calls == []


def test_remove_sources_types_from_graph_and_reports_missing(api):
    api.route("POST", "/sources/search", lambda c: SRC)
    api.route("GET", "/ui/auth/me", lambda c: {"org_id": "site-1"})
    api.route("POST", f"/ui/graphs/{G}/remove-nodes", lambda c: None)
    gone = "00000000-0000-0000-0000-000000000000"
    out = server.remove_sources(pds_luids=[L2, gone], graph_id=G)  # L2 is really a workbook
    assert api.calls[-1]["body"] == {"site_id": "site-1", "pds_luids": [], "workbook_luids": [L2]}
    assert out["removed"] == [{"id": f"workbook:c:{L2}", "name": "Overview", "type": "WORKBOOK"}]
    assert out["not_in_graph"] == [gone]


def test_remove_sources_sends_nothing_when_no_luid_is_in_the_graph(api):
    api.route("POST", "/sources/search", lambda c: SRC)
    with pytest.raises(TableauError, match="nothing removed"):
        server.remove_sources(pds_luids=["00000000-0000-0000-0000-000000000000"], graph_id=G)
    assert [c["path"] for c in api.calls] == ["/api/v1/knowledge/sources/search"]


def test_remove_sources_needs_the_site_id(api):
    api.route("POST", "/sources/search", lambda c: SRC)
    api.route("GET", "/ui/auth/me", lambda c: {})
    with pytest.raises(TableauError, match="site id"):
        server.remove_sources(pds_luids=[L1], graph_id=G)
    assert not any("remove-nodes" in c["path"] for c in api.calls)


# ---------------------------------------------------------------- server_info extras, graphs, agent, documents


def test_server_info_adds_features_and_graph_stats(api):
    api.route("GET", "/graphs", lambda c: {"graphs": [{"id": "g1", "name": "Main"}]})
    api.route(
        "GET",
        "/ui/config",
        lambda c: {"node_semantic_context_enabled": False, "demo": False, "max_graphs_per_site": 5},
    )
    api.route(
        "GET",
        "/ui/graphs",
        lambda c: [{"id": "g1", "total_nodes": 683, "connected_sources": 13, "workbooks": 3}],
    )
    api.route("GET", "/ui/graph/coverage", lambda c: {"context_coverage": 0.36})
    info = server.server_info()
    assert info["features"] == {"node_semantic_context_enabled": False, "max_graphs_per_site": 5}
    assert info["graphs"][0]["stats"] == {
        "total_nodes": 683,
        "connected_sources": 13,
        "workbooks": 3,
        "context_coverage": 0.36,
    }


def test_server_info_without_internal_api_reports_less(api):
    graphs = [{"id": "g1", "name": "Main"}]
    api.route("GET", "/graphs", lambda c: {"graphs": graphs})  # /ui/* unrouted -> 404
    info = server.server_info()
    assert "features" not in info and info["graphs"] == [{"id": "g1", "name": "Main"}]


def test_create_graph(api):
    api.route("POST", "/ui/graphs", lambda c: {"id": "new", **c["body"]})
    out = server.create_graph("  Support  ", " For support ")
    assert api.calls[0]["body"] == {"name": "Support", "description": "For support"}
    assert out["id"] == "new"


def test_create_graph_needs_a_name(api):
    with pytest.raises(TableauError):
        server.create_graph(" ")
    assert api.calls == []


def test_update_graph_patches_activates_and_reads_back(api):
    api.route("PATCH", "/ui/graphs/g1", lambda c: None)
    api.route("POST", "/ui/graphs/g1/activate", lambda c: None)
    api.route("GET", "/ui/graphs/g1", lambda c: {"id": "g1", "name": "New", "is_primary": True})
    out = server.update_graph("g1", name="New", make_primary=True)
    assert [(c["method"], c["path"]) for c in api.calls] == [
        ("PATCH", "/knowledge/api/graphs/g1"),
        ("POST", "/knowledge/api/graphs/g1/activate"),
        ("GET", "/knowledge/api/graphs/g1"),
    ]
    assert api.calls[0]["body"] == {"name": "New"} and out["is_primary"] is True


@pytest.mark.parametrize(
    "kwargs", [{"graph_id": "g1"}, {"graph_id": "g1", "name": " "}, {"graph_id": "bad id!"}]
)
def test_update_graph_validates_first(api, kwargs):
    with pytest.raises(TableauError):
        server.update_graph(**kwargs)
    assert api.calls == []


SSE = (
    'event: tool_start\ndata: {"id": "t1", "name": "search_sources"}\n\n'
    'event: tool_end\ndata: "t1"\n\n'
    'event: text_chunk\ndata: "13 data "\n\n'
    'event: text_chunk\ndata: "sources."\n\n'
    "event: done\ndata: {}\n\n"
)


def test_ask_tableau_agent_joins_chunks_and_keeps_history(api, monkeypatch):
    monkeypatch.setattr(server, "_CONVERSATIONS", {})
    api.route("POST", "/ui/chat", lambda c: _sse_resp(SSE))
    first = server.ask_tableau_agent("How many data sources?", graph_id="g1")
    assert first["answer"] == "13 data sources." and first["tools_used"] == ["search_sources"]
    assert api.calls[0]["query"] == {"graph_id": "g1"} and api.calls[0]["body"]["message_history"] == []
    server.ask_tableau_agent("And workbooks?", conversation_id=first["conversation_id"], graph_id="g1")
    assert api.calls[1]["body"]["conversation_id"] == first["conversation_id"]
    assert api.calls[1]["body"]["message_history"] == [
        {"role": "user", "content": "How many data sources?"},
        {"role": "assistant", "content": "13 data sources."},
    ]


def test_ask_tableau_agent_errors(api, monkeypatch):
    monkeypatch.setattr(server, "_CONVERSATIONS", {})
    with pytest.raises(TableauError, match="Unknown conversation_id"):
        server.ask_tableau_agent("q?", conversation_id="nope")
    api.route("POST", "/ui/chat", lambda c: _sse_resp('event: error_msg\ndata: {"text": "quota"}\n\n'))
    with pytest.raises(TableauError, match="quota"):
        server.ask_tableau_agent("q?")
    api.route("POST", "/ui/chat", lambda c: _sse_resp("event: done\ndata: {}\n\n"))
    with pytest.raises(TableauError, match="no answer"):
        server.ask_tableau_agent("q?")


def test_ask_tableau_agent_forgets_oldest_conversations(api, monkeypatch):
    monkeypatch.setattr(server, "_CONVERSATIONS", {})
    monkeypatch.setattr(server, "_MAX_CONVERSATIONS", 2)
    api.route("POST", "/ui/chat", lambda c: _sse_resp(SSE))
    ids = [server.ask_tableau_agent("q?")["conversation_id"] for _ in range(3)]
    assert list(server._CONVERSATIONS) == ids[1:]


class _Raw(Resp):
    def __init__(self, text: str):
        super().__init__(None)
        self._b = text.encode()


def _sse_resp(text: str) -> _Raw:
    return _Raw(text)


def test_document_upload_sends_multipart(api, tmp_path):
    doc = tmp_path / "glossary.md"
    doc.write_text("Profit Ratio is SUM(Profit) / SUM(Sales).\n")
    seen = {}

    def upload(c):
        seen.update(c)
        return {"id": "semctx:new", "properties": {"statements": [{"statement": "Profit Ratio ..."}]}}

    api.route("POST", "/ui/semantic-context/document", upload)
    out = server.create_semantic_context_from_document(
        str(doc), is_global=True, name="Glossary", graph_id="g1"
    )
    assert out["id"] == "semctx:new" and seen["query"] == {"graph_id": "g1"}
    assert seen["content_type"].startswith("multipart/form-data; boundary=")
    assert b'filename="glossary.md"' in seen["body"] and b"Profit Ratio is SUM" in seen["body"]
    assert b'name="name"\r\n\r\nGlossary' in seen["body"] and b"target_node_id" not in seen["body"]


def test_document_upload_validates_before_reading_or_sending(api, tmp_path):
    big = tmp_path / "big.txt"
    big.write_bytes(b"x" * (2 * 1024 * 1024 + 1))
    exe = tmp_path / "run.sh"
    exe.write_text("echo")
    cases = [
        ({"file_path": str(big), "is_global": True}, "limit"),
        ({"file_path": str(exe), "is_global": True}, "Unsupported"),
        ({"file_path": str(tmp_path / "missing.md"), "is_global": True}, "No file"),
        ({"file_path": str(big)}, "exactly one"),
    ]
    for kwargs, msg in cases:
        with pytest.raises(TableauError, match=msg):
            server.create_semantic_context_from_document(**kwargs)
    assert api.calls == []


def test_multipart_body_is_well_formed():
    body, ctype = server._multipart({"is_global": "true"}, 'a"b.md', b"hello")
    boundary = ctype.split("boundary=")[1]
    assert body.startswith(f"--{boundary}".encode()) and body.endswith(f"--{boundary}--\r\n".encode())
    assert b'name="is_global"\r\n\r\ntrue' in body and b'filename="ab.md"' in body and b"hello" in body
