"""Opt-in live tests against a real Tableau Cloud site. Skipped unless TABLEAU_LIVE is set.

Reads only, by default. Run locally against your own site (never wire secrets into public CI):

    TABLEAU_LIVE=1 TABLEAU_SERVER=... TABLEAU_SITE_CONTENT_URL=... \
    TABLEAU_PAT_NAME=... TABLEAU_PAT_SECRET=... uv run pytest -m live

The write round trip additionally needs TABLEAU_LIVE_WRITE=1. It creates ONE semantic context
on a node of the graph, updates it, and deletes it again; it never touches anything it did not
create itself.
"""

import os
import uuid

import pytest

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not os.getenv("TABLEAU_LIVE"),
        reason="set TABLEAU_LIVE=1 (with real Tableau credentials in the env) to run",
    ),
]


@pytest.fixture(scope="module")
def srv():
    from biztory_tableau_knowledge_mcp import server

    server._client = None
    yield server
    if server._client is not None:
        server._client.signout()


def test_server_info_reports_knowledge(srv):
    info = srv.server_info()
    assert info["knowledge_available"] is True, info.get("knowledge_error")
    assert any(g.get("is_primary") for g in info["graphs"])


def test_sources_and_search(srv):
    sources = srv.list_sources()
    assert sources["count"] == len(sources["sources"])
    found = srv.search_nodes("sales", limit=3)
    assert "matches" in found


def test_node_lineage_and_impact_by_id(srv):
    sources = srv.list_sources()["sources"]
    if not sources:
        pytest.skip("graph has no sources")
    node_id = sources[0]["id"]
    node = srv.get_node(node_id, include_children=False)
    assert node["id"] == node_id and "resolution" not in node
    assert srv.lineage(node_id)["node"]["id"] == node_id
    assert "affected_assets" in srv.impact_analysis(node_id)
    assert "edges" in srv.list_edges(node_id)


def test_contexts_and_health(srv):
    assert "contexts" in srv.search_semantic_contexts()
    assert "stats" in srv.graph_health(limit=3)


@pytest.mark.skipif(
    not os.getenv("TABLEAU_LIVE_WRITE"), reason="set TABLEAU_LIVE_WRITE=1 to create+delete a test context"
)
def test_write_round_trip_cleans_up_after_itself(srv):
    sources = srv.list_sources()["sources"]
    if not sources:
        pytest.skip("graph has no sources")
    tag = f"biztory-tableau-knowledge-mcp live test {uuid.uuid4().hex[:8]}"
    created = srv.create_semantic_context(
        [f"{tag}: temporary statement, safe to delete."], target_node_id=sources[0]["id"], name=tag
    )
    ctx_id = created["id"]
    try:
        assert created["target_node_id"] == sources[0]["id"]
        updated = srv.update_semantic_context(ctx_id, statements=[f"{tag}: updated statement."])
        assert updated["properties"]["statements"][0]["statement"].endswith("updated statement.")
    finally:
        srv.delete_semantic_context(ctx_id)
    remaining = srv.search_semantic_contexts(node=sources[0]["id"])["contexts"]
    assert ctx_id not in {c["id"] for c in remaining}
