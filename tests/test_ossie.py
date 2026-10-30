"""Apache Ossie export, validation and import, end to end against the fake Knowledge API."""

import json
import urllib.parse

import pytest
import yaml
from conftest import Resp, http_error, kerr

from biztory_tableau_knowledge_mcp import ossie, server
from biztory_tableau_knowledge_mcp.client import TableauError
from biztory_tableau_knowledge_mcp.tableau_calc import Untranslatable, is_aggregate, translate

# ---------------------------------------------------------------- calc translation

COLS = {
    "Sales": "sales",
    "Profit": "profit",
    "Region": "region",
    "Order Date": "order_date",
    "Order ID": "order_id",
}


def _res(name):
    if name in COLS:
        return COLS[name]
    raise Untranslatable(f"unknown field [{name}]")


@pytest.mark.parametrize(
    ("formula", "sql"),
    [
        ("SUM([Sales])", "SUM(sales)"),
        ("SUM([Profit]) / SUM([Sales])", "SUM(profit) / SUM(sales)"),
        ("COUNTD([Order ID])", "COUNT(DISTINCT order_id)"),
        ("ZN(SUM([Sales]))", "COALESCE(SUM(sales), 0)"),
        ("IFNULL([Profit], 0)", "COALESCE(profit, 0)"),
        ("ISNULL([Profit])", "(profit IS NULL)"),
        ("IIF([Profit] > 0, 'gain', 'loss')", "CASE WHEN profit > 0 THEN 'gain' ELSE 'loss' END"),
        (
            "IF [Region] == 'West' THEN 1 ELSEIF [Region] != 'East' THEN 2 ELSE 3 END",
            "CASE WHEN region = 'West' THEN 1 WHEN region <> 'East' THEN 2 ELSE 3 END",
        ),
        ("CASE [Region] WHEN 'W' THEN 1 END", "CASE region WHEN 'W' THEN 1 END"),
        ("DATEDIFF('day', [Order Date], TODAY())", "DATEDIFF(day, order_date, CURRENT_DATE)"),
        ("DATEADD('month', 3, [Order Date])", "DATEADD(month, 3, order_date)"),
        ("DATETRUNC('quarter', [Order Date])", "DATE_TRUNC('quarter', order_date)"),
        ("DATEPART('year', [Order Date])", "DATE_PART('year', order_date)"),
        ("INT([Profit])", "CAST(profit AS INTEGER)"),
        ("LEN([Region]) + CEILING([Profit])", "LENGTH(region) + CEIL(profit)"),
        ("STDEV([Sales])", "STDDEV(sales)"),
        ("#2026-01-01#", "DATE '2026-01-01'"),
        ('[Region] = "it\'s"', "region = 'it''s'"),
        ("// note\nAVG([Sales]) /* x */", "AVG(sales)"),
        ("SUM(IF [Region] = 'West' THEN [Sales] END)", "SUM(CASE WHEN region = 'West' THEN sales END)"),
    ],
)
def test_translates_the_portable_subset(formula, sql):
    assert translate(formula, _res) == sql


@pytest.mark.parametrize(
    ("formula", "reason"),
    [
        ("{INCLUDE [Region]: SUM([Sales])}", "INCLUDE"),
        ("WINDOW_SUM(SUM([Sales]))", "table calculation"),
        ("RANK(SUM([Sales]))", "table calculation"),
        ("ATTR([Region])", "ATTR"),
        ("[Parameters].[Top N]", "parameter"),
        ("[Region] + ' x'", "concatenation"),
        ("MAKEDATE(2026, 1, 1)", "MAKEDATE"),
        ("IIF([Profit] > 0, 1, 0, 2)", "IIF"),
        ("SUM([Sales]", "unbalanced"),
        ("[Unknown]", "unknown field"),
        ("[Sales] $ 2", "unexpected character"),
    ],
)
def test_refuses_what_it_cannot_translate_exactly(formula, reason):
    with pytest.raises(Untranslatable, match=reason):
        translate(formula, _res, row_level=True)


def test_fixed_lod_becomes_a_window_only_at_row_level():
    assert (
        translate("{FIXED [Region]: SUM([Sales])}", _res, row_level=True)
        == "SUM(sales) OVER (PARTITION BY region)"
    )
    assert translate("{ MAX([Order Date]) }", _res, row_level=True) == "MAX(order_date) OVER ()"
    with pytest.raises(Untranslatable, match="inside an aggregation"):
        translate("SUM({FIXED [Region]: SUM([Sales])})", _res)
    with pytest.raises(Untranslatable, match="single aggregation"):
        translate("{FIXED [Region]: SUM([Sales]) / 2}", _res, row_level=True)


def test_aggregate_detection_ignores_aggregates_inside_lods():
    assert is_aggregate("SUM([Sales]) / 2")
    assert not is_aggregate("{FIXED [Region]: SUM([Sales])}")
    assert not is_aggregate("[Sales] * 2")
    assert not is_aggregate("SUM([Sales]) $ 2")  # untokenizable: False, never raises


# ---------------------------------------------------------------- a fake data source in Knowledge

PDS = "pds:s1"
T_ORD, T_CUST = "table:orders", "table:customers"


def _field(fid, name, caption=None, **props):
    p = {"name": f"[{name}]", "datatype": "string", "role": "dimension", "hidden": False, **props}
    if caption:
        p["caption"] = caption
    return {"id": fid, "type": "FIELD", "name": name, "properties": p}


NODES = {
    PDS: {
        "id": PDS,
        "type": "PDS",
        "name": "Sales Model",
        "properties": {"description": "Orders and customers.", "luid": "L1"},
    },
    T_ORD: {"id": T_ORD, "type": "TABLE", "name": "Orders", "properties": {"database_type": "snowflake"}},
    T_CUST: {"id": T_CUST, "type": "TABLE", "name": "Customers", "properties": {}},
    "f:sales": _field(
        "f:sales", "SALES", "Sales", role="measure", datatype="real", default_aggregation="Sum"
    ),
    "f:region": _field("f:region", "REGION", "Region", description="Sales region."),
    "f:date": _field("f:date", "Order Date", datatype="date"),
    "f:secret": _field("f:secret", "SECRET", "Secret", hidden=True),
    "f:segment": _field("f:segment", "SEGMENT", "Segment"),
    "c:bucket": _field(
        "c:bucket", "Calculation_1", "Size Bucket", formula="IIF([SALES] > 100, 'big', 'small')"
    ),
    "c:total": _field(
        "c:total", "Calculation_2", "Net Sales", role="measure", datatype="real", formula="SUM([SALES])"
    ),
    "c:share": _field(
        "c:share", "Calculation_3", "West Share", role="measure", formula="[Calculation_2] / 2"
    ),
    "c:lod": _field(
        "c:lod", "Calculation_4", "Region Total", role="measure", formula="{FIXED [REGION]: SUM([SALES])}"
    ),
    "c:cross": _field(
        "c:cross",
        "Calculation_5",
        "Sales per Segment",
        role="measure",
        formula="SUM([SALES]) / COUNTD([SEGMENT])",
    ),
    "c:tc": _field("c:tc", "Calculation_6", "Running", role="measure", formula="RUNNING_SUM(SUM([SALES]))"),
}
CHILDREN = {
    PDS: [
        ("TABLE", T_ORD),
        ("TABLE", T_CUST),
        *(("FIELD", c) for c in ("c:bucket", "c:total", "c:share", "c:lod", "c:cross", "c:tc")),
    ],
    T_ORD: [("FIELD", f) for f in ("f:sales", "f:region", "f:date", "f:secret")],
    T_CUST: [("FIELD", "f:segment")],
}
CONTEXTS = [
    {"id": "semctx:a", "type": "SEMANTIC_CONTEXT", "name": "Defs", "target_node_id": "f:sales",
     "properties": {"is_global": False, "statements": [{"id": "s1", "statement": "Sales excludes returns."}]}},
    {"id": "semctx:p", "type": "SEMANTIC_CONTEXT", "name": "Model", "target_node_id": PDS,
     "properties": {"is_global": False, "statements": [{"id": "s2", "statement": "Use for revenue questions."}]}},
    {"id": "semctx:g", "type": "SEMANTIC_CONTEXT", "name": "Site", "target_node_id": None,
     "properties": {"is_global": True, "statements": [{"id": "s3", "statement": "Fiscal year starts in April."}]}},
]  # fmt: skip


def _serve(api, contexts=CONTEXTS):
    for nid, node in NODES.items():

        def get(call, node=node):
            kids = []
            if call["query"].get("include_children", "true") == "true":
                kids = [
                    {"id": c, "type": t, "name": NODES[c]["name"], "edge_type": "HAS"}
                    for t, c in CHILDREN.get(node["id"], [])
                ]
            return {**node, "semantic_statements": [], "connected_nodes": kids}

        api.route("GET", "/nodes/" + urllib.parse.quote(nid, safe=""), get)
    api.route("POST", "/semantic-contexts/search", lambda c: contexts)


def _export(api, **kw):
    _serve(api)
    return server.export_to_ossie(PDS, **kw)


def _ds(model, name):
    return next(d for d in model["datasets"] if d["name"] == name)


def _by_name(items, name):
    return next(i for i in items if i["name"] == name)


# ---------------------------------------------------------------- export


def test_export_builds_a_valid_ossie_model(api):
    out = _export(api)
    assert out["validation"] == {"valid": True, "errors": [], "warnings": []}
    assert out["file_name"] == "sales_model.ossie.yaml"
    assert out["ossie"].startswith("# Apache Ossie 0.2.0.dev0")
    assert '\nversion: "0.2.0.dev0"\n' in out["ossie"]
    model = yaml.safe_load(out["ossie"])
    assert model["name"] == "sales_model" and model["description"] == "Orders and customers."
    assert "Use for revenue questions." in model["ai_context"]["instructions"]
    assert "Site-wide: Fiscal year starts in April." in model["ai_context"]["instructions"]
    assert [d["name"] for d in model["datasets"]] == ["orders", "customers"]
    assert out["summary"]["datasets"] == 2


def test_export_maps_column_fields(api):
    orders = _ds(yaml.safe_load(_export(api)["ossie"]), "orders")
    sales = _by_name(orders["fields"], "sales")
    assert sales["expression"] == {
        "sql": "SALES",
        "dialects": [{"dialect": "ANSI_SQL", "expression": "SALES"}],
    }
    assert "dimension" not in sales and sales["datatype"] == "Float"
    assert sales["ai_context"] == {"instructions": "Sales excludes returns."}
    stash = json.loads(sales["custom_extensions"][0]["data"])
    assert sales["custom_extensions"][0]["vendor_name"] == "TABLEAU"
    assert stash["node_id"] == "f:sales" and stash["default_aggregation"] == "Sum"
    assert _by_name(orders["fields"], "region")["dimension"] == {"is_time": False}
    assert _by_name(orders["fields"], "region")["description"] == "Sales region."
    order_date = _by_name(orders["fields"], "order_date")
    assert order_date["dimension"] == {"is_time": True} and order_date["expression"]["sql"] == '"Order Date"'
    assert "secret" not in [f["name"] for f in orders["fields"]]


def test_export_places_calcs_as_fields_and_metrics(api):
    out = _export(api)
    model = yaml.safe_load(out["ossie"])
    orders = _ds(model, "orders")
    bucket = _by_name(orders["fields"], "size_bucket")
    assert bucket["expression"]["sql"] == "CASE WHEN SALES > 100 THEN 'big' ELSE 'small' END"
    assert bucket["expression"]["dialects"] == [
        {"dialect": "TABLEAU", "expression": "IIF([Sales] > 100, 'big', 'small')"}
    ]
    lod = _by_name(orders["fields"], "region_total")
    assert lod["expression"]["sql"] == "SUM(SALES) OVER (PARTITION BY REGION)"
    net = _by_name(orders["metrics"], "net_sales")
    assert net["expression"]["sql"] == "SUM(sales)"
    share = _by_name(orders["metrics"], "west_share")  # references an aggregate calc: inlined
    assert share["expression"]["sql"] == "(SUM(sales)) / 2"
    assert share["expression"]["dialects"][0]["expression"] == "[Net Sales] / 2"
    total = _by_name(orders["metrics"], "total_sales")  # from the default aggregation
    assert total["expression"]["sql"] == "SUM(sales)"
    assert json.loads(total["custom_extensions"][0]["data"])["derived_from"] == "f:sales"
    cross = _by_name(model["metrics"], "sales_per_segment")  # two tables: model-scoped, qualified
    assert cross["expression"]["sql"] == "SUM(orders.sales) / COUNT(DISTINCT customers.segment)"
    running = _by_name(orders["metrics"], "running")
    assert "sql" not in running["expression"]
    codes = {(x["code"], x["element"]) for x in out["losses"]}
    assert ("untranslated_calc", "metric orders.running") in codes
    assert ("relationships_unavailable", "relationships") in codes
    assert ("hidden_fields_skipped", "fields") in codes
    assert sum(1 for c, _ in codes if c == "source_unresolved") == 2


def test_export_options(api):
    model = yaml.safe_load(_export(api, include_hidden=True, default_aggregation_metrics=False)["ossie"])
    orders = _ds(model, "orders")
    assert "secret" in [f["name"] for f in orders["fields"]]
    assert "total_sales" not in [m["name"] for m in orders["metrics"]]


def test_export_refuses_a_non_datasource(api):
    _serve(api)
    with pytest.raises(TableauError, match="not a published data source"):
        server.export_to_ossie(T_ORD)


def test_name_helpers():
    assert ossie.snake("Order Date (copy) 2") == "order_date_copy_2"
    assert ossie.snake("2024 Sales") == "f_2024_sales" and ossie.snake("!!") == "unnamed"
    assert ossie.sql_ident("Order Date") == '"Order Date"' and ossie.sql_ident("SALES") == "SALES"
    ns = ossie._Namespace()
    assert [ns.claim("a"), ns.claim("A"), ns.claim("a")] == ["a", "A_2", "a_3"]


# ---------------------------------------------------------------- validation

GOOD = {
    "version": "0.2.0.dev0",
    "name": "m",
    "datasets": [{"name": "d", "source": "db.s.t", "fields": [{"name": "id", "expression": {"sql": "id"}}]}],
}


def test_validate_accepts_the_minimal_model():
    out = server.validate_ossie(yaml.safe_dump(GOOD))
    assert out["valid"] and out["version"] == "0.2.0.dev0"


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda m: m.pop("name"), "'name' is a required property"),
        (lambda m: m["datasets"][0].update(description=None), "None is not of type"),
        (lambda m: m["datasets"].append(dict(m["datasets"][0])), "duplicate dataset"),
        (lambda m: m["datasets"][0].update(primary_key=["nope"]), "key column 'nope'"),
        (
            lambda m: m.update(
                relationships=[
                    {"name": "r", "from": "d", "to": "x", "from_columns": ["id"], "to_columns": ["id"]}
                ]
            ),
            "'x' does not exist",
        ),
        (
            lambda m: m.update(
                relationships=[
                    {"name": "r", "from": "d", "to": "d", "from_columns": ["id"], "to_columns": ["id", "id"]}
                ]
            ),
            "differ in length",
        ),
        (
            lambda m: m["datasets"][0].update(metrics=[{"name": "t", "expression": {"sql": "SUM(d.id)"}}]),
            "qualified name",
        ),
    ],
)
def test_validate_reports_errors(mutate, message):
    model = json.loads(json.dumps(GOOD))
    mutate(model)
    out = server.validate_ossie(json.dumps(model))
    assert not out["valid"]
    assert any(message in e["message"] for e in out["errors"]), out["errors"]


def test_validate_converts_0_1_1_documents():
    old = {"version": "0.1.1", "semantic_model": [{**{k: v for k, v in GOOD.items() if k != "version"}}]}
    out = server.validate_ossie(yaml.safe_dump(old))
    assert out["valid"] and "Converted from Ossie 0.1.1" in out["notes"][0]
    two = {
        "version": "0.1.1",
        "semantic_model": [dict(old["semantic_model"][0]), {**old["semantic_model"][0], "name": "n"}],
    }
    with pytest.raises(TableauError, match="pass model_name"):
        server.validate_ossie(yaml.safe_dump(two))
    assert server.validate_ossie(yaml.safe_dump(two), model_name="n")["valid"]
    with pytest.raises(TableauError, match="No model named"):
        server.validate_ossie(yaml.safe_dump(two), model_name="zz")


def test_validate_rejects_bad_input():
    with pytest.raises(TableauError, match="Not valid YAML"):
        server.validate_ossie("a: [")
    with pytest.raises(TableauError, match="mapping at the top level"):
        server.validate_ossie("- 1")
    with pytest.raises(TableauError, match="larger than"):
        server.validate_ossie("x" * (ossie.MAX_DOCUMENT_BYTES + 1))


def test_safe_loader_refuses_python_tags():
    with pytest.raises(TableauError, match="Not valid YAML"):
        server.validate_ossie("!!python/object/apply:os.system ['echo hi']")


def test_extension_codec_reads_strings_and_objects():
    assert ossie._read_ext({"custom_extensions": [{"vendor_name": "TABLEAU", "data": '{"a": 1}'}]}) == {
        "a": 1
    }
    assert ossie._read_ext({"custom_extensions": [{"vendor_name": "TABLEAU", "data": {"a": 1}}]}) == {"a": 1}
    assert ossie._read_ext({"custom_extensions": [{"vendor_name": "TABLEAU", "data": "not json"}]}) == {}
    assert ossie._read_ext({"custom_extensions": [{"vendor_name": "DBT", "data": "{}"}]}) == {}


# ---------------------------------------------------------------- import


def test_round_trip_plan_matches_by_exported_ids(api):
    text = _export(api)["ossie"]
    plan = server.import_from_ossie(text)
    assert plan["applied"] is False and plan["valid"]
    assert plan["target_datasource"] == {"id": PDS, "name": "Sales Model"}
    assert (
        plan["operations"] == [] and plan["unmatched"] == []
    )  # nothing new: an unedited round trip is a no-op
    assert plan["summary"]["statements_already_in_tableau"] >= 4

    model = yaml.safe_load(text)  # now enrich the exported YAML elsewhere and bring it back
    sales = _by_name(_ds(model, "orders")["fields"], "sales")
    sales["ai_context"] = {"instructions": "Sales excludes returns.", "synonyms": ["revenue", "turnover"]}
    plan = server.import_from_ossie(yaml.safe_dump(model, sort_keys=False))
    ops = {o["node_id"]: o for o in plan["operations"]}
    assert list(ops) == ["f:sales"]
    assert ops["f:sales"]["action"] == "create" and ops["f:sales"]["context_name"] == "ossie:sales_model"
    assert ops["f:sales"]["node_name"] == "Sales"
    assert ops["f:sales"]["elements"] == ["field orders.sales (exported id)"]
    assert ops["f:sales"]["statements"] == ["Also known as: revenue, turnover."]
    assert not any(
        c["method"] == "POST" and c["path"].endswith("/semantic-contexts") for c in api.calls
    )  # no writes


def test_plan_composes_statements_and_matches_by_name(api):
    _serve(api)
    doc = {
        "version": "0.2.0.dev0", "name": "crm", "description": "CRM model.",
        "datasets": [
            {"name": "orders", "source": "db.s.orders", "fields": [
                {"name": "Region", "expression": {"sql": "region"}, "description": "Where it sold.",
                 "ai_context": {"synonyms": ["territory", "area"], "examples": ["Sales by region?"]}},
                {"name": "ghost", "expression": {"sql": "g"}},
            ], "metrics": [{"name": "net_sales", "expression": {"sql": "SUM(sales)"}, "ai_context": "Use for revenue."}]},
            {"name": "nowhere", "source": "db.s.x"},
        ],
    }  # fmt: skip
    plan = server.import_from_ossie(yaml.safe_dump(doc), target_datasource_id=PDS)
    ops = {o["node_id"]: o for o in plan["operations"]}
    assert ops[PDS]["statements"] == ["CRM model."]
    assert ops["f:region"]["statements"] == [
        "Where it sold.",
        "Also known as: territory, area.",
        "Example question: Sales by region?",
    ]
    assert ops["c:total"]["statements"] == [
        "Use for revenue.",
        "Defined in the crm semantic model as: SUM(sales)",
    ]
    assert {u["element"] for u in plan["unmatched"]} == {"field orders.ghost", "dataset nowhere"}


def test_plan_needs_a_target_for_foreign_models(api):
    _serve(api)
    with pytest.raises(TableauError, match="target_datasource_id"):
        server.import_from_ossie(yaml.safe_dump(GOOD))


def test_invalid_model_is_not_planned(api):
    bad = {**GOOD, "datasets": []}
    plan = server.import_from_ossie(yaml.safe_dump(bad), target_datasource_id=PDS)
    assert plan["valid"] is False and plan["operations"] == []
    assert api.calls == []


def test_reimport_updates_or_skips_existing_contexts(api):
    text = _enriched(api)
    existing = [
        *CONTEXTS,
        {"id": "semctx:o1", "type": "SEMANTIC_CONTEXT", "name": "ossie:sales_model", "target_node_id": "f:sales",
         "properties": {"statements": [{"id": "x", "statement": "Gross sales in EUR."}]}},
        {"id": "semctx:o2", "type": "SEMANTIC_CONTEXT", "name": "ossie:sales_model", "target_node_id": PDS,
         "properties": {"statements": [{"id": "y", "statement": "old"}]}},
    ]  # fmt: skip
    _serve(api, contexts=existing)
    plan = server.import_from_ossie(text)
    ops = {o["node_id"]: o for o in plan["operations"]}
    assert ops["f:sales"]["action"] == "unchanged" and ops["f:sales"]["context_id"] == "semctx:o1"
    assert ops[PDS]["action"] == "update" and ops[PDS]["context_id"] == "semctx:o2"


def _enriched(api):
    """An exported model with new descriptions on the model, a field and a metric."""
    model = yaml.safe_load(_export(api)["ossie"])
    model["description"] = "Curated sales model."
    _by_name(_ds(model, "orders")["fields"], "sales")["description"] = "Gross sales in EUR."
    _by_name(_ds(model, "orders")["metrics"], "net_sales")["description"] = "Sales after returns."
    return yaml.safe_dump(model, sort_keys=False)


def test_apply_writes_creates_and_updates_and_reports_failures(api):
    text = _enriched(api)
    existing = [
        *CONTEXTS,
        {
            "id": "semctx:o2",
            "type": "SEMANTIC_CONTEXT",
            "name": "ossie:sales_model",
            "target_node_id": PDS,
            "properties": {"statements": []},
        },
    ]
    _serve(api, contexts=existing)

    def create(call):
        if call["body"]["target_node_id"] == "c:total":
            return http_error("u", 403, kerr("tableau_forbidden"))
        return Resp({"id": "semctx:new-" + call["body"]["target_node_id"]}, 201)

    api.route("POST", "/semantic-contexts", create)
    api.route("PATCH", "/semantic-contexts/semctx%3Ao2", lambda c: {"id": "semctx:o2"})
    out = server.import_from_ossie(text, apply=True)
    assert out["applied"] is True
    results = {r["node_id"]: r for r in out["results"]}
    assert results[PDS]["result"] == "updated"
    assert results["f:sales"] == {
        "node_id": "f:sales",
        "result": "created",
        "context_id": "semctx:new-f:sales",
    }
    assert results["c:total"]["result"] == "failed" and "Creator" in results["c:total"]["error"]
    assert out["applied_summary"]["failed"] == 1 and out["applied_summary"]["updated"] == 1
    posts = [c for c in api.calls if c["method"] == "POST" and c["path"].endswith("/semantic-contexts")]
    assert all(p["body"]["name"] == "ossie:sales_model" and p["body"]["statements"] for p in posts)


def test_long_text_is_split_to_the_statement_limit():
    long = ("This is a sentence. " * 120).strip()
    parts = ossie._chunk([long, "tiny", long])
    assert all(5 <= len(p) <= 1000 for p in parts)
    assert len(parts) == len(set(parts))  # duplicates dropped


# ---------------------------------------------------------------- guide and prompts


def test_guide_topics():
    out = server.ossie_guide()
    assert out["topics"][0] == "overview" and "Apache Ossie" in out["content"]
    assert "ossie-snowflake" in server.ossie_guide("Converters")["content"]
    assert "error" in server.ossie_guide("nope")


def test_prompts_are_registered():
    prompts = {p.name: p for p in server.mcp._prompt_manager.list_prompts()}
    assert set(prompts) == {
        "export_datasource_to_ossie",
        "import_ossie_into_tableau_knowledge",
        "write_business_preferences",
    }
    assert "export_to_ossie" in server.export_datasource_prompt("Superstore")
    assert "apply=true" in server.import_ossie_prompt("model.ossie.yaml", "Superstore")


def _disabled(call):
    """What BiztoryPulse answered (2026-10-30) to any node-scoped create."""
    if "target_node_id" in call["body"]:
        return http_error(
            "u", 403, kerr("node_semantic_context_disabled", "Node-scoped semantic context is disabled")
        )
    return Resp({"id": "semctx:global-1"}, 201)


def test_auto_scope_falls_back_to_one_global_context(api):
    text = _enriched(api)
    plan = server.import_from_ossie(text)
    assert plan["scope"] == "auto" and plan["global_fallback"]["scope"] == "global"
    assert "global_fallback" in plan["notes"][-1]
    assert "Sales Model / Sales: Gross sales in EUR." in plan["global_fallback"]["statements"]
    assert "Sales Model: Curated sales model." in plan["global_fallback"]["statements"]
    api.route("POST", "/semantic-contexts", _disabled)
    out = server.import_from_ossie(text, apply=True)
    assert "fallback_to_global" in out
    assert out["results"] == [{"node_id": None, "result": "created", "context_id": "semctx:global-1"}]
    posts = [c for c in api.calls if c["method"] == "POST" and c["path"].endswith("/semantic-contexts")]
    assert len(posts) == 2  # one refused node attempt, then the global context
    assert posts[1]["body"]["is_global"] is True and posts[1]["body"]["name"] == "ossie:sales_model"


def test_node_scope_never_falls_back(api):
    text = _enriched(api)
    api.route("POST", "/semantic-contexts", _disabled)
    out = server.import_from_ossie(text, scope="node", apply=True)
    assert "fallback_to_global" not in out and "global_fallback" not in out
    assert out["applied_summary"]["failed"] == out["summary"]["create"]
    assert "graph-wide" in out["results"][0]["error"]  # the hint explains what to do


def test_global_scope_plans_and_updates_the_global_context(api):
    text = _enriched(api)
    plan = server.import_from_ossie(text, scope="global")
    assert len(plan["operations"]) == 1 and plan["operations"][0]["node_name"] == "(whole graph)"
    statements = plan["operations"][0]["statements"]
    existing = [*CONTEXTS, {"id": "semctx:og", "type": "SEMANTIC_CONTEXT", "name": "ossie:sales_model", "target_node_id": None,
                            "properties": {"is_global": True, "statements": [{"id": "z", "statement": s} for s in statements]}}]  # fmt: skip
    _serve(api, contexts=existing)
    again = server.import_from_ossie(text, scope="global")
    assert (
        again["operations"][0]["action"] == "unchanged"
        and again["operations"][0]["context_id"] == "semctx:og"
    )


def test_scope_is_checked(api):
    with pytest.raises(TableauError, match="scope must be one of"):
        server.import_from_ossie(yaml.safe_dump(GOOD), target_datasource_id=PDS, scope="everywhere")
