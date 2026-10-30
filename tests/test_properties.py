"""Property tests for the two trust-boundary helpers: id encoding and graph_id checking."""

import urllib.parse

from hypothesis import given
from hypothesis import strategies as st

from biztory_tableau_knowledge_mcp.client import segment
from biztory_tableau_knowledge_mcp.config import GRAPH_ID_PATTERN


@given(st.text(min_size=1))
def test_an_id_is_always_exactly_one_path_segment(node_id):
    enc = segment(node_id)
    # Nothing that could end the segment, start a query/fragment, or break the request line.
    assert not set(enc) & set("/?#&= \r\n\t")
    assert urllib.parse.unquote(enc) == node_id  # and the server sees the id unchanged


@given(st.text())
def test_graph_id_pattern_only_admits_url_safe_ids(gid):
    if GRAPH_ID_PATTERN.match(gid):
        assert 1 <= len(gid) <= 128
        assert urllib.parse.quote(gid, safe="") == gid
