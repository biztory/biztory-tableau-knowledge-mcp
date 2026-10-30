"""Tableau Knowledge <-> Apache Ossie (incubating, formerly OSI) semantic models.

Export builds one Ossie ``0.2.0.dev0`` model per published data source from what the Knowledge
graph holds: logical tables -> datasets, column fields -> fields, row-level calcs -> fields,
aggregate calcs -> metrics, semantic-context statements -> ``ai_context``. Tableau-only detail is
kept in a ``TABLEAU`` custom extension so a model round-trips; anything Ossie cannot hold is
reported as a loss, never dropped silently.

Import is the reverse, within what the Knowledge API can write: semantic contexts. It matches
each Ossie element to a node (exact ids from a previous export first, then names), and writes
one context per node, named ``ossie:<model>`` so re-imports update instead of duplicating.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from importlib.resources import files
from typing import Any

import yaml
from jsonschema import Draft202012Validator

from . import __version__
from .client import TableauClient, TableauError, segment
from .tableau_calc import Untranslatable, is_aggregate, ref_name, referenced_names, translate

OSSIE_VERSION = "0.2.0.dev0"
VENDOR = "TABLEAU"
MAX_DOCUMENT_BYTES = 5_000_000
_STATEMENT_MIN, _STATEMENT_MAX = 5, 1000

DATATYPES = {
    "string": "String", "integer": "Integer", "real": "Float", "boolean": "Boolean",
    "date": "Date", "datetime": "DateTime",
}  # fmt: skip
TIME_TYPES = {"Date", "Time", "DateTime", "DateTimeTz"}
# Tableau default aggregation -> (Ossie aggregate template, metric name prefix)
DEFAULT_AGGREGATIONS = {
    "sum": ("SUM({})", "total_"), "avg": ("AVG({})", "avg_"), "min": ("MIN({})", "min_"),
    "max": ("MAX({})", "max_"), "count": ("COUNT({})", "count_"),
    "countd": ("COUNT(DISTINCT {})", "distinct_count_"), "median": ("MEDIAN({})", "median_"),
}  # fmt: skip

_SCHEMA: dict[str, Any] = json.loads(
    files(__package__).joinpath("schemas/ossie-schema-0.2.0.dev0.json").read_text(encoding="utf-8")
)
_VALIDATOR = Draft202012Validator(_SCHEMA)


# ---------------------------------------------------------------- names and helpers


def snake(name: str) -> str:
    s = re.sub(r"[^0-9A-Za-z]+", "_", (name or "").strip()).strip("_").lower()
    s = re.sub(r"_+", "_", s)
    if not s:
        return "unnamed"
    return f"f_{s}" if s[0].isdigit() else s


def norm(name: str) -> str:
    """Loose key for matching names across tools: case, spaces and punctuation ignored."""
    return re.sub(r"[^0-9a-z]+", "", (name or "").lower())


def sql_ident(column: str) -> str:
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", column):
        return column
    return '"' + column.replace('"', '""') + '"'


class _Namespace:
    """Hands out unique snake_case names (case-insensitive), suffixing _2, _3 on collisions."""

    def __init__(self) -> None:
        self.taken: set[str] = set()

    def claim(self, wanted: str) -> str:
        base, n, name = wanted, 1, wanted
        while name.lower() in self.taken:
            n += 1
            name = f"{base}_{n}"
        self.taken.add(name.lower())
        return name


def _ext(data: dict[str, Any]) -> list[dict[str, str]]:
    return [{"vendor_name": VENDOR, "data": json.dumps(data, sort_keys=True, ensure_ascii=False)}]


def _read_ext(obj: dict[str, Any]) -> dict[str, Any]:
    """This vendor's stash from ``custom_extensions`` (``data`` as a JSON string, or an object
    once apache/ossie#522 lands). Other vendors' entries are left alone."""
    for ext in obj.get("custom_extensions") or []:
        if isinstance(ext, dict) and ext.get("vendor_name") == VENDOR:
            data = ext.get("data")
            if isinstance(data, dict):
                return data
            if isinstance(data, str):
                try:
                    parsed = json.loads(data)
                except ValueError:
                    return {}
                return parsed if isinstance(parsed, dict) else {}
    return {}


def _loss(losses: list[dict[str, str]], severity: str, element: str, code: str, message: str) -> None:
    losses.append({"severity": severity, "element": element, "code": code, "message": message})


# ---------------------------------------------------------------- load a data source from Knowledge


def load_datasource(c: TableauClient, pds_id: str, graph: dict[str, Any]) -> dict[str, Any]:
    """Fetch a published data source with its logical tables, fields and semantic contexts."""

    def node(node_id: str, children: bool) -> dict[str, Any]:
        result: dict[str, Any] = c.knowledge(
            "GET", f"/nodes/{segment(node_id)}", query={**graph, "include_children": children}
        )
        return result

    pds = node(pds_id, True)
    if pds.get("type") != "PDS":
        raise TableauError(
            f"{pds.get('name')!r} is a {pds.get('type')}, not a published data source (PDS). "
            "Use list_sources to find a data source id."
        )
    kids = pds.get("connected_nodes") or []
    tables = [node(t["id"], True) for t in kids if t.get("type") == "TABLE"]
    field_refs = [
        (t["id"], f["id"]) for t in tables for f in t.get("connected_nodes") or [] if f.get("type") == "FIELD"
    ]
    field_refs += [(None, f["id"]) for f in kids if f.get("type") == "FIELD"]
    with ThreadPoolExecutor(max_workers=4) as pool:  # ponytail: 4 workers; the API rate-limits bursts
        fetched = list(pool.map(lambda r: node(r[1], False), field_refs))
    fields = [{**f, "table_node_id": table_id} for (table_id, _), f in zip(field_refs, fetched, strict=True)]
    contexts = c.knowledge("POST", "/semantic-contexts/search", query=graph, body={}) or []
    return {"pds": pds, "tables": tables, "fields": fields, "contexts": contexts}


def _statements_by_target(contexts: Iterable[dict[str, Any]]) -> dict[str | None, list[dict[str, Any]]]:
    by: dict[str | None, list[dict[str, Any]]] = defaultdict(list)
    for ctx in contexts:
        props = ctx.get("properties") or {}
        target = None if props.get("is_global") else ctx.get("target_node_id")
        by[target].append(ctx)
    return by


def _context_text(contexts: list[dict[str, Any]]) -> list[str]:
    return [
        str(s.get("statement")).strip()
        for ctx in contexts
        for s in (ctx.get("properties") or {}).get("statements") or []
        if s.get("statement")
    ]


def _ai_context(contexts: list[dict[str, Any]], prefix: str = "") -> str | None:
    text = _context_text(contexts)
    return "\n".join(prefix + t for t in text) if text else None


def _context_stash(contexts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "id": ctx.get("id"),
            "name": ctx.get("name"),
            "type": ctx.get("type"),
            "statements": [s.get("statement") for s in (ctx.get("properties") or {}).get("statements") or []],
        }
        for ctx in contexts
    ]


# ---------------------------------------------------------------- export


def export_datasource(
    c: TableauClient,
    pds_id: str,
    graph: dict[str, Any],
    *,
    include_hidden: bool = False,
    default_aggregation_metrics: bool = True,
) -> dict[str, Any]:
    data = load_datasource(c, pds_id, graph)
    pds = data["pds"]
    losses: list[dict[str, str]] = []
    ctx_by = _statements_by_target(data["contexts"])
    props = pds.get("properties") or {}

    tables = data["tables"]
    dataset_names = _Namespace()
    datasets: list[dict[str, Any]] = []
    ds_by_table: dict[str, dict[str, Any]] = {}
    spaces: dict[str, _Namespace] = {}  # per dataset: fields + dataset metrics share one namespace
    for t in tables:
        tp = t.get("properties") or {}
        name = dataset_names.claim(snake(t.get("name", "")))
        ds: dict[str, Any] = {"name": name, "source": t.get("name") or name, "fields": []}
        ctx = ctx_by.get(t["id"], [])
        if ai := _ai_context(ctx):
            ds["ai_context"] = {"instructions": ai}
        ds["custom_extensions"] = _ext(
            {
                "node_id": t["id"],
                "logical_table_id": tp.get("logical_table_id"),
                "database_type": tp.get("database_type"),
                "contexts": _context_stash(ctx),
            }
        )
        _loss(
            losses, "warning", f"dataset {name}", "source_unresolved",
            f"Tableau Knowledge does not expose the physical table behind logical table {t.get('name')!r} "
            f"({tp.get('database_type') or 'unknown'} connection); `source` is set to the logical table "
            "name. Replace it with database.schema.table (or a query) before using the model elsewhere.",
        )  # fmt: skip
        datasets.append(ds)
        ds_by_table[t["id"]] = ds
        spaces[name] = _Namespace()
    if not datasets:
        raise TableauError(
            f"{pds.get('name')!r} has no logical tables in Tableau Knowledge; nothing to export."
        )
    if len(datasets) > 1:
        _loss(
            losses, "warning", "relationships", "relationships_unavailable",
            "Tableau Knowledge does not expose the join keys between logical tables "
            f"({', '.join(d['name'] for d in datasets)}). Add `relationships` by hand: from = the many "
            "side, to = the one side, from_columns/to_columns = field names.",
        )  # fmt: skip

    fields = data["fields"]
    by_tname = {
        ref_name(f["properties"].get("name") or f"[{f.get('name')}]"): f
        for f in fields
        if f.get("properties")
    }
    by_caption = {f["properties"].get("caption"): f for f in fields if f.get("properties", {}).get("caption")}

    def lookup(ref: str) -> dict[str, Any] | None:
        return by_tname.get(ref) or by_caption.get(ref)

    def caption(f: dict[str, Any]) -> str:
        p = f.get("properties") or {}
        return str(p.get("caption") or ref_name(p.get("name") or f"[{f.get('name')}]"))

    def column(f: dict[str, Any]) -> str:
        return ref_name((f.get("properties") or {}).get("name") or f"[{f.get('name')}]")

    def is_calc(f: dict[str, Any]) -> bool:
        return bool((f.get("properties") or {}).get("formula"))

    def readable(formula: str) -> str:
        """The formula as the calc editor shows it: internal names like [Calculation_123] -> [Caption]."""
        out = formula.replace("\r\n", "\n")
        for r in referenced_names(formula):
            f = lookup(r)
            if f and caption(f) != r:
                out = out.replace(f"[{r}]", "[" + caption(f).replace("]", "]]") + "]")
        return out

    def aggregate(f: dict[str, Any], seen: frozenset[str] = frozenset()) -> bool:
        """An aggregate calc, or one built on one (Tableau treats both as aggregates)."""
        formula = (f.get("properties") or {}).get("formula")
        if not formula:
            return False
        if is_aggregate(formula):
            return True
        return any(
            g is not None and g["id"] not in seen and aggregate(g, seen | {f["id"]})
            for g in map(lookup, referenced_names(formula))
        )

    def tables_of(f: dict[str, Any], seen: frozenset[str] = frozenset()) -> set[str]:
        """Logical tables a field reads from, through calc references."""
        if not is_calc(f):
            return {f["table_node_id"]} if f.get("table_node_id") else set()
        out: set[str] = set()
        for r in referenced_names(f["properties"]["formula"]):
            g = lookup(r)
            if g and g["id"] not in seen:
                out |= tables_of(g, seen | {f["id"]})
        return out

    exported: dict[str, tuple[str, str]] = {}  # field node id -> (dataset name, ossie name)
    visible = [
        f for f in fields if f.get("properties") and (include_hidden or not f["properties"].get("hidden"))
    ]
    hidden = len([f for f in fields if f.get("properties", {}).get("hidden")]) if not include_hidden else 0
    if hidden:
        _loss(
            losses, "info", "fields", "hidden_fields_skipped",
            f"{hidden} hidden Tableau field(s) skipped. Pass include_hidden=true to export them.",
        )  # fmt: skip

    # 1) column fields
    for f in visible:
        if is_calc(f) or not f.get("table_node_id"):
            continue
        p, ds = f["properties"], ds_by_table[f["table_node_id"]]
        name = spaces[ds["name"]].claim(snake(caption(f)))
        expr = sql_ident(column(f))
        field: dict[str, Any] = {
            "name": name,
            "expression": {"sql": expr, "dialects": [{"dialect": "ANSI_SQL", "expression": expr}]},
        }
        _field_common(field, f, ctx_by, losses)
        ds["fields"].append(field)
        exported[f["id"]] = (ds["name"], name)

    # 2) row-level calcs become fields (before metrics, so metrics can reference them by name)
    calcs = [f for f in visible if is_calc(f)]
    row_calcs = [f for f in calcs if not aggregate(f)]
    agg_calcs = [f for f in calcs if aggregate(f)]
    first = datasets[0]
    for f in row_calcs:
        home = sorted(tables_of(f))
        ds = ds_by_table[home[0]] if home else first
        name = spaces[ds["name"]].claim(snake(caption(f)))
        field = {"name": name, "expression": _calc_expression(f, readable)}
        if len(home) > 1:
            _loss(
                losses, "warning", f"field {ds['name']}.{name}", "cross_table_row_calc",
                "Row-level calculation reads columns of several logical tables; Ossie fields may only use "
                "their own dataset's columns. Kept in the TABLEAU dialect only.",
            )  # fmt: skip
        else:
            src_table = home[0] if home else None
            sql = _try_translate(
                f, losses, f"field {ds['name']}.{name}",
                partial(_row_ref, table=src_table, lookup=lookup, column=column, aggregate=aggregate), row_level=True,
            )  # fmt: skip
            if sql:
                field["expression"]["sql"] = sql
        _field_common(field, f, ctx_by, losses)
        ds["fields"].append(field)
        exported[f["id"]] = (ds["name"], name)

    # 3) aggregate calcs become metrics (dataset-scoped when they read one table, else model-scoped)
    model_metrics: list[dict[str, Any]] = []
    model_space = _Namespace()
    for f in agg_calcs:
        home = sorted(tables_of(f))
        scoped: dict[str, Any] | None = ds_by_table[home[0]] if len(home) == 1 else (None if home else first)
        space = model_space if scoped is None else spaces[scoped["name"]]
        name = space.claim(snake(caption(f)))
        element = f"metric {name}" if scoped is None else f"metric {scoped['name']}.{name}"
        metric: dict[str, Any] = {"name": name, "expression": _calc_expression(f, readable)}
        resolve = partial(_metric_ref, qualify=scoped is None, lookup=lookup, exported=exported)
        sql = _try_translate(f, losses, element, resolve)
        if sql:
            metric["expression"]["sql"] = sql
        _metric_common(metric, f, ctx_by)
        if scoped is None:
            model_metrics.append(metric)
        else:
            scoped.setdefault("metrics", []).append(metric)

    # 4) Tableau's default aggregation on plain measures -> explicit metrics
    if default_aggregation_metrics:
        for f in visible:
            p = f["properties"]
            agg = str(p.get("default_aggregation") or "").lower()
            if (
                is_calc(f)
                or p.get("role") != "measure"
                or f["id"] not in exported
                or agg not in DEFAULT_AGGREGATIONS
            ):
                continue
            ds_name, field_name = exported[f["id"]]
            template, prefix = DEFAULT_AGGREGATIONS[agg]
            ds = next(d for d in datasets if d["name"] == ds_name)
            name = spaces[ds_name].claim(prefix + field_name)
            sql = template.format(field_name)
            tableau = (
                ("COUNTD" if agg == "countd" else agg.upper()) + "([" + caption(f).replace("]", "]]") + "])"
            )
            ds.setdefault("metrics", []).append(
                {
                    "name": name,
                    "expression": {
                        "sql": sql,
                        "dialects": [
                            {"dialect": "ANSI_SQL", "expression": sql},
                            {"dialect": "TABLEAU", "expression": tableau},
                        ],
                    },
                    "custom_extensions": _ext({"derived_from": f["id"], "default_aggregation": agg}),
                }
            )

    # Non-column, non-calc fields Knowledge lists under the data source (e.g. groups, sets, bins).
    for f in visible:
        if not is_calc(f) and not f.get("table_node_id"):
            _loss(
                losses, "warning", f"field {caption(f)}", "unsupported_field_type",
                f"Tableau field of type {f['properties'].get('field_type')!r} has no Ossie equivalent; not exported.",
            )  # fmt: skip

    pds_ctx = ctx_by.get(pds["id"], [])
    global_ctx = ctx_by.get(None, [])
    model: dict[str, Any] = {"version": OSSIE_VERSION, "name": snake(pds.get("name", ""))}
    if props.get("description"):
        model["description"] = str(props["description"])
    ai = "\n".join(filter(None, [_ai_context(pds_ctx), _ai_context(global_ctx, "Site-wide: ")]))
    if ai:
        model["ai_context"] = {"instructions": ai}
    model["datasets"] = datasets
    model["relationships"] = []
    model["metrics"] = model_metrics
    model["custom_extensions"] = _ext(
        {
            "pds_id": pds["id"],
            "luid": props.get("luid"),
            "graph_id": graph.get("graph_id"),
            "datasource_name": pds.get("name"),
            "owner": props.get("owner"),
            "certified": props.get("certified"),
            "database_type": props.get("database_type"),
            "is_extract": props.get("is_extract"),
            "contexts": _context_stash(pds_ctx),
            "global_contexts": _context_stash(global_ctx),
            "exported_by": f"biztory-tableau-knowledge-mcp {__version__}",
        }
    )
    for ds in datasets:  # keep each dataset's keys in spec order, metrics after fields
        if "metrics" in ds:
            ds["metrics"] = ds.pop("metrics")
        ds["custom_extensions"] = ds.pop("custom_extensions")

    validation = validate_document(model)
    text = render_yaml(model, pds.get("name", ""))
    counts = {
        "datasets": len(datasets),
        "fields": sum(len(d["fields"]) for d in datasets),
        "dataset_metrics": sum(len(d.get("metrics", [])) for d in datasets),
        "model_metrics": len(model_metrics),
        "losses": len(losses),
    }
    return {
        "file_name": f"{model['name']}.ossie.yaml",
        "ossie_version": OSSIE_VERSION,
        "summary": counts,
        "validation": validation,
        "losses": losses,
        "ossie": text,
    }


def _field_common(
    field: dict[str, Any],
    f: dict[str, Any],
    ctx_by: dict[str | None, list[dict[str, Any]]],
    losses: list[dict[str, str]],
) -> None:
    p = f["properties"]
    dt = DATATYPES.get(str(p.get("datatype") or "").lower())
    if p.get("role") == "dimension":
        field["dimension"] = {"is_time": dt in TIME_TYPES}
    if p.get("description"):
        field["description"] = str(p["description"])
    if dt:
        field["datatype"] = dt
    elif p.get("datatype"):
        field["datatype"] = "Opaque"
    ctx = ctx_by.get(f["id"], [])
    if ai := _ai_context(ctx):
        field["ai_context"] = {"instructions": ai}
    field["custom_extensions"] = _ext(_field_stash(f, ctx))


def _metric_common(
    metric: dict[str, Any], f: dict[str, Any], ctx_by: dict[str | None, list[dict[str, Any]]]
) -> None:
    p = f["properties"]
    if p.get("description"):
        metric["description"] = str(p["description"])
    if dt := DATATYPES.get(str(p.get("datatype") or "").lower()):
        metric["datatype"] = dt
    ctx = ctx_by.get(f["id"], [])
    if ai := _ai_context(ctx):
        metric["ai_context"] = {"instructions": ai}
    metric["custom_extensions"] = _ext(_field_stash(f, ctx))


def _field_stash(f: dict[str, Any], ctx: list[dict[str, Any]]) -> dict[str, Any]:
    p = f["properties"]
    keep = (
        "name",
        "caption",
        "role",
        "datatype",
        "semantic_type",
        "default_aggregation",
        "hidden",
        "field_type",
        "formula",
    )
    out: dict[str, Any] = {"node_id": f["id"], **{k: p[k] for k in keep if p.get(k) not in (None, "")}}
    if ctx:
        out["contexts"] = _context_stash(ctx)
    return out


def _calc_expression(f: dict[str, Any], readable: Callable[[str], str]) -> dict[str, Any]:
    return {"dialects": [{"dialect": "TABLEAU", "expression": readable(f["properties"]["formula"])}]}


def _try_translate(
    f: dict[str, Any],
    losses: list[dict[str, str]],
    element: str,
    resolve: Callable[[str], str],
    *,
    row_level: bool = False,
) -> str | None:
    try:
        return translate(f["properties"]["formula"], resolve, row_level=row_level)
    except Untranslatable as e:
        _loss(
            losses, "warning", element, "untranslated_calc",
            f"Kept in the TABLEAU dialect only (no portable SQL): {e}. SQL-based targets will need a hand translation.",
        )  # fmt: skip
        return None


def _row_ref(
    ref: str,
    table: str | None,
    lookup: Callable[[str], dict[str, Any] | None],
    column: Callable[[dict[str, Any]], str],
    aggregate: Callable[[dict[str, Any]], bool],
    depth: int = 0,
) -> str:
    """A reference inside a row-level calc: source columns of the same table, row calcs inlined."""
    f = lookup(ref)
    if f is None:
        raise Untranslatable(f"unknown field [{ref}]")
    formula = (f.get("properties") or {}).get("formula")
    if not formula:
        if f.get("table_node_id") != table:
            raise Untranslatable(f"[{ref}] belongs to another logical table")
        return sql_ident(column(f))
    if aggregate(f):
        raise Untranslatable(f"row-level calc references aggregate [{ref}]")
    if depth > 10:
        raise Untranslatable("calculation references nest too deep")
    try:
        inner = translate(
            formula, lambda r: _row_ref(r, table, lookup, column, aggregate, depth + 1), row_level=True
        )
    except Untranslatable as e:
        raise Untranslatable(f"via [{(f.get('properties') or {}).get('caption') or ref}]: {e}") from None
    return f"({inner})"


def _metric_ref(
    ref: str,
    qualify: bool,
    lookup: Callable[[str], dict[str, Any] | None],
    exported: dict[str, tuple[str, str]],
    depth: int = 0,
) -> str:
    """A reference inside a metric: exported fields by name (qualified when model-scoped);
    other aggregate calcs inlined, because Ossie metrics cannot reference metrics."""
    f = lookup(ref)
    if f is None:
        raise Untranslatable(f"unknown field [{ref}]")
    if f["id"] in exported:
        ds, name = exported[f["id"]]
        return f"{ds}.{name}" if qualify else name
    formula = (f.get("properties") or {}).get("formula")
    if formula and depth <= 10:
        try:
            inner = translate(formula, lambda r: _metric_ref(r, qualify, lookup, exported, depth + 1))
        except Untranslatable as e:
            raise Untranslatable(f"via [{(f.get('properties') or {}).get('caption') or ref}]: {e}") from None
        return f"({inner})"
    raise Untranslatable(f"[{ref}] is not exported (hidden? pass include_hidden=true)")


# ---------------------------------------------------------------- YAML in/out, normalize, validate


class _Dumper(yaml.SafeDumper):
    pass


def _str_presenter(dumper: yaml.SafeDumper, value: str) -> yaml.ScalarNode:
    if "\n" in value:
        return dumper.represent_scalar("tag:yaml.org,2002:str", value, style="|")
    return dumper.represent_scalar("tag:yaml.org,2002:str", value)


_Dumper.add_representer(str, _str_presenter)


def render_yaml(model: dict[str, Any], source_name: str = "") -> str:
    header = (
        f"# Apache Ossie {OSSIE_VERSION} semantic model"
        + (f" exported from Tableau data source {source_name!r}" if source_name else "")
        + f"\n# by biztory-tableau-knowledge-mcp {__version__}. Review `losses` before porting.\n"
    )
    body = yaml.dump(
        model, Dumper=_Dumper, sort_keys=False, allow_unicode=True, width=100, default_flow_style=False
    )
    # The spec asks for a quoted version string, so no YAML parser reads it as anything else.
    return header + body.replace(f"version: {OSSIE_VERSION}\n", f'version: "{OSSIE_VERSION}"\n', 1)


def parse_document(text: str) -> Any:
    """Parse Ossie YAML or JSON text (safe loader; no custom tags, no code)."""
    if len(text.encode("utf-8")) > MAX_DOCUMENT_BYTES:
        raise TableauError(f"Document is larger than {MAX_DOCUMENT_BYTES // 1_000_000} MB.")
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise TableauError(f"Not valid YAML/JSON: {str(e)[:300]}") from None


def normalize(doc: Any, model_name: str | None = None) -> tuple[dict[str, Any], list[str]]:
    """Return a 0.2.0-shaped model. 0.1.1 documents (``semantic_model: [...]``) are converted;
    with several models, ``model_name`` picks one (never silently the first)."""
    notes: list[str] = []
    if not isinstance(doc, dict):
        raise TableauError("An Ossie document is a mapping at the top level (version, name, datasets, ...).")
    if "semantic_model" not in doc:
        return doc, notes
    models = doc.get("semantic_model") or []
    if not isinstance(models, list) or not models:
        raise TableauError("`semantic_model` is empty.")
    names = [m.get("name") for m in models if isinstance(m, dict)]
    if len(models) > 1 and not model_name:
        raise TableauError(
            f"This 0.1.1 document holds {len(models)} models {names}; pass model_name to pick one."
        )
    chosen = next(
        (m for m in models if isinstance(m, dict) and (not model_name or m.get("name") == model_name)), None
    )
    if chosen is None:
        raise TableauError(f"No model named {model_name!r}; the document has {names}.")
    notes.append(
        f"Converted from Ossie {doc.get('version', '0.1.1')} (semantic_model list) to the {OSSIE_VERSION} shape."
    )
    return {"version": OSSIE_VERSION, **{k: v for k, v in chosen.items() if k != "version"}}, notes


def validate_document(model: Any) -> dict[str, Any]:
    """Structural (official JSON Schema) and semantic checks (names, references, keys)."""
    errors: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []
    for e in sorted(_VALIDATOR.iter_errors(model), key=lambda e: list(e.absolute_path)):
        errors.append({"path": "/" + "/".join(str(p) for p in e.absolute_path), "message": e.message[:300]})
    if not isinstance(model, dict):
        return {"valid": False, "errors": errors, "warnings": warnings}

    def dupes(items: Iterable[Any], where: str, what: str) -> None:
        seen: set[str] = set()
        for it in items:
            name = it.get("name") if isinstance(it, dict) else None
            if isinstance(name, str):
                if name.lower() in seen:
                    errors.append({"path": where, "message": f"duplicate {what} name {name!r}"})
                seen.add(name.lower())

    datasets = [d for d in model.get("datasets") or [] if isinstance(d, dict)]
    dupes(datasets, "/datasets", "dataset")
    fields_of: dict[str, set[str]] = {}
    for i, ds in enumerate(datasets):
        fnames = {
            f["name"].lower()
            for f in ds.get("fields") or []
            if isinstance(f, dict) and isinstance(f.get("name"), str)
        }
        fields_of[str(ds.get("name", "")).lower()] = fnames
        dupes([*(ds.get("fields") or []), *(ds.get("metrics") or [])], f"/datasets/{i}", "field/metric")
        for key in [*([ds["primary_key"]] if ds.get("primary_key") else []), *(ds.get("unique_keys") or [])]:
            for col in key if isinstance(key, list) else []:
                if isinstance(col, str) and col.lower() not in fnames:
                    errors.append(
                        {"path": f"/datasets/{i}", "message": f"key column {col!r} is not a declared field"}
                    )
        ds_names = {str(d.get("name", "")).lower() for d in datasets}
        for j, m in enumerate(ds.get("metrics") or []):
            sql = ((m.get("expression") or {}).get("sql") or "") if isinstance(m, dict) else ""
            for q in re.findall(r"\b([A-Za-z_][A-Za-z0-9_]*)\.[A-Za-z_\"]", sql):
                if q.lower() in ds_names:
                    errors.append(
                        {
                            "path": f"/datasets/{i}/metrics/{j}",
                            "message": f"dataset-scoped metric uses qualified name {q}.…; use bare field names",
                        }
                    )
                    break
    dupes(model.get("metrics") or [], "/metrics", "model metric")
    rels = [r for r in model.get("relationships") or [] if isinstance(r, dict)]
    dupes(rels, "/relationships", "relationship")
    for i, r in enumerate(rels):
        for side, cols in (("from", "from_columns"), ("to", "to_columns")):
            ds_name = str(r.get(side, "")).lower()
            if ds_name not in fields_of:
                errors.append(
                    {
                        "path": f"/relationships/{i}",
                        "message": f"`{side}` dataset {r.get(side)!r} does not exist",
                    }
                )
                continue
            for col in r.get(cols) or []:
                if isinstance(col, str) and col.lower() not in fields_of[ds_name]:
                    errors.append(
                        {
                            "path": f"/relationships/{i}",
                            "message": f"{cols}: {col!r} is not a field of {r.get(side)!r}",
                        }
                    )
        if len(r.get("from_columns") or []) != len(r.get("to_columns") or []):
            errors.append(
                {"path": f"/relationships/{i}", "message": "from_columns and to_columns differ in length"}
            )
    for ds in datasets:
        if not any(isinstance(f, dict) for f in ds.get("fields") or []):
            warnings.append({"path": f"/datasets/{ds.get('name')}", "message": "dataset has no fields"})
    return {"valid": not errors, "errors": errors[:200], "warnings": warnings[:200]}


# ---------------------------------------------------------------- import (plan, then apply)


def _statements_for(obj: dict[str, Any], kind: str, model_name: str, include_definitions: bool) -> list[str]:
    out: list[str] = []
    if isinstance(obj.get("description"), str):
        out.append(obj["description"])
    ai = obj.get("ai_context")
    if isinstance(ai, str):
        out.append(ai)
    elif isinstance(ai, dict):
        if isinstance(ai.get("instructions"), str):  # export joins statements with newlines
            out += [line for line in ai["instructions"].splitlines() if line.strip()]
        syn = [s for s in ai.get("synonyms") or [] if isinstance(s, str)]
        if syn:
            out.append("Also known as: " + ", ".join(syn) + ".")
        out += [f"Example question: {q}" for q in ai.get("examples") or [] if isinstance(q, str)]
    if include_definitions and kind == "metric":
        expr = obj.get("expression") or {}
        sql = expr.get("sql") or next(
            (
                d.get("expression")
                for d in expr.get("dialects") or []
                if isinstance(d, dict) and d.get("expression")
            ),
            None,
        )
        if sql:
            out.append(f"Defined in the {model_name} semantic model as: {sql}")
    if kind == "relationship":
        pairs = ", ".join(
            f"{a} = {obj.get('to')}.{b}"
            for a, b in zip(obj.get("from_columns") or [], obj.get("to_columns") or [], strict=False)
        )
        out.append(f"Relates to {obj.get('to')} (many-to-one) on {pairs}.")
    return _chunk(out)


def _chunk(texts: Iterable[str]) -> list[str]:
    """Fit the API's 5-1000 character statement limit: split long text at sentence ends."""
    out: list[str] = []
    for text in texts:
        text = " ".join(str(text).split()) if "\n" not in str(text) else str(text).strip()
        while len(text) > _STATEMENT_MAX:
            cut = max(text.rfind(". ", 0, _STATEMENT_MAX), text.rfind("\n", 0, _STATEMENT_MAX))
            cut = cut + 1 if cut > _STATEMENT_MIN else _STATEMENT_MAX
            out.append(text[:cut].strip())
            text = text[cut:].strip()
        if len(text) >= _STATEMENT_MIN:
            out.append(text)
    seen: dict[str, None] = {}
    for t in out:
        seen.setdefault(t, None)
    return list(seen)


def plan_import(
    c: TableauClient,
    text: str,
    graph: dict[str, Any],
    *,
    target_datasource_id: str | None,
    model_name: str | None,
    include_definitions: bool,
) -> dict[str, Any]:
    doc, notes = normalize(parse_document(text), model_name)
    validation = validate_document(doc)
    if not validation["valid"]:
        return {"applied": False, "valid": False, "validation": validation, "notes": notes, "operations": []}
    model_stash = _read_ext(doc)
    target = (target_datasource_id or "").strip() or model_stash.get("pds_id")
    if not target:
        raise TableauError(
            "This model was not exported from Tableau Knowledge, so pass target_datasource_id: "
            "the exact id of the published data source to annotate (from list_sources)."
        )
    if target_datasource_id and model_stash.get("pds_id") and target_datasource_id != model_stash["pds_id"]:
        notes.append(
            f"Model was exported from {model_stash['pds_id']}; importing into {target_datasource_id} as asked."
        )
    data = load_datasource(c, target, graph)
    pds, mname = data["pds"], str(doc.get("name"))

    tables = {t["id"]: t for t in data["tables"]}
    fields = {f["id"]: f for f in data["fields"] if f.get("properties")}

    def keys_of(node: dict[str, Any]) -> set[str]:
        p = node.get("properties") or {}
        names = [node.get("name"), p.get("caption"), ref_name(p["name"]) if p.get("name") else None]
        return {k for n in names if n for k in (norm(str(n)),)}

    def match(
        stash: dict[str, Any], name: str, pool: dict[str, dict[str, Any]], *extra: str
    ) -> tuple[dict[str, Any] | None, str]:
        for key in ("node_id", "derived_from"):
            if stash.get(key) in pool:
                return pool[stash[key]], "exported id"
        wanted = {norm(n) for n in (name, *extra) if n}
        hits = [n for n in pool.values() if keys_of(n) & wanted]
        if len(hits) == 1:
            return hits[0], "name"
        return None, ("ambiguous name" if hits else "no match")

    ops: dict[str, dict[str, Any]] = {}
    unmatched: list[dict[str, str]] = []
    context_name = f"ossie:{mname}"

    # What Tableau already says about a node (its description, its other contexts, site-wide
    # contexts) is not written again: an unedited round trip writes nothing.
    def text_key(s: Any) -> str:
        return " ".join(str(s).split()).lower().removeprefix("site-wide: ")

    known: dict[str | None, set[str]] = defaultdict(set)
    for ctx in data["contexts"]:
        if ctx.get("name") != context_name:
            target = None if (ctx.get("properties") or {}).get("is_global") else ctx.get("target_node_id")
            known[target] |= {text_key(s) for s in _context_text([ctx])}
    already_known = 0

    def add(node: dict[str, Any], element: str, how: str, statements: list[str]) -> None:
        nonlocal already_known
        desc = text_key((node.get("properties") or {}).get("description") or "")
        fresh = [
            s for s in statements if (k := text_key(s)) not in known[None] | known[node["id"]] and k != desc
        ]
        already_known += len(statements) - len(fresh)
        if not fresh:
            return
        statements = fresh
        op = ops.setdefault(
            node["id"],
            {
                "node_id": node["id"],
                "node_name": (node.get("properties") or {}).get("caption") or node.get("name"),
                "node_type": node.get("type"),
                "elements": [],
                "statements": [],
            },
        )
        op["elements"].append(f"{element} ({how})")
        op["statements"] = _chunk([*op["statements"], *statements])

    add(pds, f"model {mname}", "target", _statements_for(doc, "model", mname, include_definitions))
    table_of_dataset: dict[str, dict[str, Any]] = {}
    for ds in doc.get("datasets") or []:
        st = _read_ext(ds)
        table, how = match(st, ds.get("name", ""), tables, str(ds.get("source", "")).split(".")[-1])
        if table is None:
            unmatched.append({"element": f"dataset {ds.get('name')}", "reason": how})
            continue
        table_of_dataset[ds["name"]] = table
        add(table, f"dataset {ds['name']}", how, _statements_for(ds, "dataset", mname, include_definitions))
        in_table = {
            i: f
            for i, f in fields.items()
            if f.get("table_node_id") == table["id"] or not f.get("table_node_id")
        }
        for fd in ds.get("fields") or []:
            node, how = match(_read_ext(fd), fd.get("name", ""), in_table)
            if node is None:
                unmatched.append({"element": f"field {ds['name']}.{fd.get('name')}", "reason": how})
                continue
            add(
                node,
                f"field {ds['name']}.{fd['name']}",
                how,
                _statements_for(fd, "field", mname, include_definitions),
            )
        for m in ds.get("metrics") or []:
            node, how = match(_read_ext(m), m.get("name", ""), fields)
            if node is None:
                unmatched.append({"element": f"metric {ds['name']}.{m.get('name')}", "reason": how})
                continue
            add(
                node,
                f"metric {ds['name']}.{m['name']}",
                how,
                _statements_for(m, "metric", mname, include_definitions and how != "exported id"),
            )
    for m in doc.get("metrics") or []:
        node, how = match(_read_ext(m), m.get("name", ""), fields)
        if node is None:
            unmatched.append({"element": f"metric {m.get('name')}", "reason": how})
            continue
        add(
            node, f"metric {m['name']}", how,
            _statements_for(m, "metric", mname, include_definitions and how != "exported id"),
        )  # fmt: skip
    for r in doc.get("relationships") or []:
        table = table_of_dataset.get(r.get("from"))
        if table is None:
            unmatched.append(
                {"element": f"relationship {r.get('name')}", "reason": "its `from` dataset was not matched"}
            )
            continue
        add(
            table,
            f"relationship {r['name']}",
            "from dataset",
            _statements_for(r, "relationship", mname, include_definitions),
        )

    existing = {
        ctx.get("target_node_id"): ctx
        for ctx in data["contexts"]
        if ctx.get("name") == context_name
        and ctx.get("type") == "SEMANTIC_CONTEXT"
        and ctx.get("target_node_id")
    }
    operations = []
    for op in ops.values():
        prior = existing.get(op["node_id"])
        if prior is None:
            action = "create"
        else:
            current = [s.get("statement") for s in (prior.get("properties") or {}).get("statements") or []]
            action = "unchanged" if current == op["statements"] else "update"
            op["context_id"] = prior.get("id")
        operations.append({"action": action, "context_name": context_name, **op})
    summary = {a: sum(1 for o in operations if o["action"] == a) for a in ("create", "update", "unchanged")}
    return {
        "applied": False,
        "valid": True,
        "model": mname,
        "target_datasource": {"id": pds["id"], "name": pds.get("name")},
        "summary": {**summary, "unmatched": len(unmatched), "statements_already_in_tableau": already_known},
        "operations": operations,
        "unmatched": unmatched,
        "validation": validation,
        "notes": notes,
    }


def apply_plan(c: TableauClient, plan: dict[str, Any], graph: dict[str, Any]) -> dict[str, Any]:
    results = []
    for op in plan["operations"]:
        if op["action"] == "unchanged":
            results.append(
                {"node_id": op["node_id"], "result": "unchanged", "context_id": op.get("context_id")}
            )
            continue
        body: dict[str, Any] = {"statements": [{"statement": s} for s in op["statements"]]}
        try:
            if op["action"] == "update":
                c.knowledge(
                    "PATCH", f"/semantic-contexts/{segment(op['context_id'])}", query=graph, body=body
                )
                results.append(
                    {"node_id": op["node_id"], "result": "updated", "context_id": op["context_id"]}
                )
            else:
                created = c.knowledge(
                    "POST", "/semantic-contexts", query=graph,
                    body={**body, "target_node_id": op["node_id"], "name": op["context_name"]},
                )  # fmt: skip
                results.append(
                    {"node_id": op["node_id"], "result": "created", "context_id": (created or {}).get("id")}
                )
        except TableauError as e:  # keep going: report each failure next to its node
            results.append({"node_id": op["node_id"], "result": "failed", "error": str(e)})
    done = {
        k: sum(1 for r in results if r["result"] == k) for k in ("created", "updated", "unchanged", "failed")
    }
    return {**plan, "applied": True, "results": results, "applied_summary": done}
