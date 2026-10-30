"""Business Preferences: format, limits, wording, and field names against the fake Knowledge API."""

import urllib.parse

import pytest

from biztory_tableau_knowledge_mcp import server
from biztory_tableau_knowledge_mcp.client import TableauError
from biztory_tableau_knowledge_mcp.preferences import BASELINE, lint

GOOD = "\n".join(
    [
        "# Bookings means [Closed Won Amount]; use it for every bookings question.",
        "# A deal is large when [Closed Won Amount] is at least 60% of target; use only this value.",
        BASELINE["query"],
        BASELINE["failure"],
    ]
)


def rules_hit(result):
    return {i["rule"] for i in result["issues"]}


def test_a_clean_file_passes():
    out = lint(GOOD)
    assert out["issues"] == [] and out["suggested_lines"] == []
    assert out["stats"]["rules"] == 4 and out["stats"]["rule_limit"] == 50
    assert out["stats"]["characters_left"] == 8000 - len(GOOD)


@pytest.mark.parametrize(
    ("line", "severity"),
    [
        (" # leading space", "error"),
        ("#no space", "error"),
        ("## double hash", "error"),
        ("plain text", "error"),
        ("#", "error"),
        ("#  two spaces", "warning"),
    ],
)
def test_format_slips_are_caught(line, severity):
    out = lint(f"{GOOD}\n{line}")
    hits = [i for i in out["issues"] if i["rule"] == "format"]
    assert hits and hits[0]["severity"] == severity and hits[0]["line"] == 5


def test_limits_per_product():
    many = "\n".join(
        f"# Rule number {i} says something specific about retry and query use." for i in range(51)
    )
    assert any(i["rule"] == "limit" and i["severity"] == "error" for i in lint(many)["issues"])
    assert not any(i["rule"] == "limit" for i in lint(many, "next")["issues"])  # Next has no rule count limit
    long = "# " + "x" * 7950
    assert any(
        i["rule"] == "limit" and i["severity"] == "warning" for i in lint(long)["issues"]
    )  # within headroom
    assert any(i["rule"] == "limit" and i["severity"] == "error" for i in lint("# " + "x" * 8001)["issues"])
    assert not any(i["rule"] == "limit" for i in lint("# " + "x" * 8001, "next")["issues"])
    with pytest.raises(TableauError, match="product"):
        lint(GOOD, "server")


@pytest.mark.parametrize(
    ("line", "rule"),
    [
        ("# Definitions:", "header"),
        ("# ===========", "header"),
        ("# Please show me revenue by region.", "conversational"),
        ("# What is a large deal?", "conversational"),
        ("# A large discount is above 20%.", "threshold"),
        ("# Format Sales as currency.", "display_in_query"),
        ("# Never tell users to contact an administrator.", "administrator"),
        ("# The data has no seller hierarchy.", "absence"),
        ("# Sort losses descending.", "ordering"),
        ("# Report the average profit ratio per customer.", "average_of_ratios"),
    ],
)
def test_wording_rules(line, rule):
    assert rule in rules_hit(lint(f"{GOOD}\n{line}"))


@pytest.mark.parametrize(
    "line",
    [
        "# A large discount is above 20%; use only this value.",
        "# Add $ signs only in the written answer, never in a query.",
        "# Seller names are available; only territory and manager are missing.",
        "# Report the ratio of totals, not the average of ratios.",
    ],
)
def test_well_written_lines_pass(line):
    assert lint(f"{GOOD}\n{line}")["issues"] == []


def test_duplicates_contradictions_and_blank_lines():
    out = lint(
        f"{GOOD}\n\n# Show two decimal places in the written answer.\n# Show whole dollars, no cents, in the written answer.\n{GOOD.splitlines()[0]}"
    )
    hits = rules_hit(out)
    assert {"blank_line", "contradiction", "duplicate"} <= hits


def test_missing_baseline_rules_are_suggested():
    out = lint("# Bookings means [Closed Won Amount].")
    assert out["suggested_lines"] == [BASELINE["query"], BASELINE["failure"]]
    assert {i["rule"] for i in out["issues"]} == {"baseline"}


def test_empty_text_returns_the_rulebook():
    out = server.check_business_preferences()
    assert "Every line starts with" in out["rulebook"] and out["limits"]["cloud"] == (50, 8000)
    assert out["baseline_rules"] == list(BASELINE.values())


# ---------------------------------------------------------------- fields from the Knowledge graph

DASH, WB, EDS, TBL = "dashboard:d", "workbook:w", "eds:e", "table:t"


def _f(fid, caption, **props):
    return {
        "id": fid,
        "type": "FIELD",
        "name": caption,
        "properties": {"name": f"[{fid}]", "caption": caption, "hidden": False, **props},
    }


FIELDS = {
    "f1": _f("f1", "Closed Won Amount", role="measure"),
    "f2": _f("f2", "Account ID (Account)"),
    "f3": _f("f3", "Account ID (Opportunity)"),
    "f4": _f("f4", "Amount", role="measure"),
    "f5": _f("f5", "Existing Customer Helper"),
    "f6": _f("f6", "Region Secret", hidden=True),
    "Calculation_9": _f("Calculation_9", "Win Rate", formula="SUM([f1]) / 2"),
}
NODES = {
    DASH: {"id": DASH, "type": "DASHBOARD", "name": "Pipeline", "properties": {}},
    WB: {"id": WB, "type": "WORKBOOK", "name": "Sales", "properties": {}},
    EDS: {"id": EDS, "type": "EMBEDDED_DATASOURCE", "name": "Sales data", "properties": {}},
    TBL: {"id": TBL, "type": "TABLE", "name": "Opportunity", "properties": {}},
    **FIELDS,
}
CHILDREN = {
    EDS: [("TABLE", TBL), ("FIELD", "Calculation_9")],
    TBL: [("FIELD", f) for f in ("f1", "f2", "f3", "f4", "f5", "f6")],
}
EDGES = {
    DASH: [{"type": "CONTAINS", "direction": "incoming", "connected_node": {"id": WB, "type": "WORKBOOK"}}],
    WB: [
        {"type": "CONTAINS", "direction": "outgoing", "connected_node": {"id": DASH, "type": "DASHBOARD"}},
        {
            "type": "CONTAINS",
            "direction": "outgoing",
            "connected_node": {"id": EDS, "type": "EMBEDDED_DATASOURCE"},
        },
    ],
}


def _serve(api):
    for nid, node in NODES.items():

        def get(call, node=node):
            kids = [{"id": c, "type": t, "name": NODES[c]["name"]} for t, c in CHILDREN.get(node["id"], [])]
            return {
                **node,
                "semantic_statements": [],
                "connected_nodes": kids if call["query"].get("include_children") == "true" else [],
            }

        api.route("GET", "/nodes/" + urllib.parse.quote(nid, safe=""), get)
    api.route(
        "POST",
        "/edges/search",
        lambda c: {"node_id": c["body"]["node_id"], "name": "", "edges": EDGES.get(c["body"]["node_id"], [])},
    )
    api.route("POST", "/semantic-contexts/search", lambda c: [])


def test_field_names_are_checked_against_the_dashboard(api):
    _serve(api)
    text = "\n".join(
        [
            "# Bookings means [Closed Won Amount].",  # exact: fine
            "# Use [closed won amount] for totals.",  # case
            "# Ignore [Region Secret].",  # hidden
            "# Win rate is [Calculation_9].",  # internal name
            "# Count deals with [Account ID].",  # unknown, did you mean
            '# "Closed Won Amout" is the bookings figure.',  # quoted near miss
            '# "pipeline" means open deals.',  # quoted business term: not a field, not flagged
            BASELINE["query"],
            BASELINE["failure"],
        ]
    )
    out = server.check_business_preferences(text, target=DASH)
    assert out["fields"]["target"]["type"] == "DASHBOARD" and out["fields"]["data_sources"] == ["Sales data"]
    by_line = {(i["line"], i["rule"]) for i in out["issues"]}
    assert (2, "field_case") in by_line and (3, "field_hidden") in by_line
    assert (4, "field_internal_name") in by_line and (5, "field_unknown") in by_line
    assert (6, "field_near_miss") in by_line
    assert not any(i["line"] in (1, 7) for i in out["issues"])
    unknown = next(i for i in out["issues"] if i["rule"] == "field_unknown")
    assert "Account ID (Account)" in unknown["message"]
    assert out["ok"] is False and out["fields"]["references_checked"] == 6
    prep = {(i["rule"], i["message"].split("'")[1]) for i in out["fields"]["workbook_preparation"]}
    assert ("duplicate_field_name", "Account ID (Account)") in prep
    assert ("generic_caption", "Amount") in prep
    assert ("helper_field_visible", "Existing Customer Helper") in prep


def test_issues_are_sorted_by_line(api):
    _serve(api)
    out = server.check_business_preferences("# Use [Nope] here.\n#bad", target=EDS)
    lines = [i["line"] for i in out["issues"]]
    assert lines == sorted(lines, key=lambda x: (x is None, x or 0))


def test_target_must_lead_to_data(api):
    _serve(api)
    with pytest.raises(TableauError, match="pass a dashboard, sheet, workbook or data source"):
        server.check_business_preferences(GOOD, target=TBL)
    api.route("POST", "/edges/search", lambda c: {"node_id": c["body"]["node_id"], "name": "", "edges": []})
    with pytest.raises(TableauError, match="Could not find the workbook"):
        server.check_business_preferences(GOOD, target=DASH)
