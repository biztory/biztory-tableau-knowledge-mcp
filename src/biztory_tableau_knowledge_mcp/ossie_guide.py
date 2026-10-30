"""Curated Apache Ossie reference served by the ``ossie_guide`` tool.

Distilled from apache/ossie @ 7a8de20 (spec 0.2.0.dev0) and apache/ossie-converters @ bb900e3,
read 2026-10-30. Update the pins and facts here when the spec or converters move.
"""

from __future__ import annotations

SPEC_COMMIT = "7a8de20f850dad3934e307a1ac7425b09e9ca89f"
CONVERTERS_COMMIT = "bb900e317af61d1ae1f0c55d72dbd026698031d5"
OSSIE_PYTHON_COMMIT = "01a5488d840deda309aac68cc0b312c195369b8d"

GUIDE: dict[str, str] = {
    "overview": """\
# Apache Ossie (incubating), formerly Open Semantic Interchange (OSI)
A vendor-neutral YAML standard for semantic models (datasets, fields, metrics, relationships, AI
context) so BI tools, semantic layers and AI agents share one definition of "Revenue".
- Site: https://ossie.apache.org . Spec: github.com/apache/ossie core-spec/spec.md, schema
  core-spec/ossie-schema.json (JSON Schema 2020-12), expression_language.md.
- Versions: the only tag is osi-0.1.1-rc1 (May 2026; `semantic_model:` list, closed dialect
  enum). `main` is 0.2.0.dev0 (flat root, one model per document) and says "do not depend on this
  version in production". Every converter already uses 0.2.0.dev0, so this server writes
  0.2.0.dev0 and also reads 0.1.1 (it converts the shape on import/validate).
- Nothing is published yet: no converter or Python SDK on PyPI/Maven/npm, no CLI release. The Go
  `ossie` CLI never implemented convert/validate and is being removed (apache/ossie PR #656).
- File naming is not normative; `*.ossie.yaml` is what the compliance fixtures use.
Topics: model, expressions, tableau, relationships, ai_context, extensions, validation,
converters, porting.""",
    "model": """\
# Object model (0.2.0.dev0)
Root (one semantic model per document): version (const "0.2.0.dev0", quote it), name,
description, ai_context, datasets (>=1), relationships, metrics (model-scoped),
custom_extensions. Required: version, name, datasets.
- Dataset: name, source ("db.schema.table" or a query; required), primary_key [fields],
  unique_keys [[fields]], description, ai_context, fields, metrics (dataset-scoped),
  custom_extensions.
- Field: name, expression (required), dimension {is_time}, label (a category tag like
  "filter", NOT a display name), description, datatype, ai_context, custom_extensions.
  A field with a `dimension` object is a dimension; without it, a fact. is_time defaults to true
  for Date/Time/DateTime/DateTimeTz datatypes.
- Metric: name, expression (required), description, datatype, ai_context, custom_extensions.
- Relationship: name, from (many side), to (one side), from_columns, to_columns (field names,
  positional, equal length), ai_context, custom_extensions. No description, no cardinality.
- Expression: `sql` (portable OSSIE_SQL_2026) and/or `dialects` [{dialect, expression}].
- datatype: String, Integer, Decimal (exact), Float, Boolean, Date, Time, DateTime (no tz),
  DateTimeTz, Opaque. Omit when unknown.
Rules: additionalProperties false everywhere except ai_context; null is never valid (omit the
key); names/sources/expressions are non-empty. Field expressions reference SOURCE COLUMNS only;
keys and relationship columns are FIELD names; dataset-scoped metrics use bare field names of
their dataset; model-scoped metrics must use dataset.field. Fields cannot reference fields and
metrics cannot reference metrics: inline instead. Fields and dataset metrics share a namespace.""",
    "expressions": """\
# Expressions
OSSIE_SQL_2026 = ANSI SQL:2003 core + FILTER (WHERE ...) on aggregates. Scalar expressions only:
no SELECT/FROM/JOIN/GROUP BY/subqueries. Window functions with OVER (...) are allowed.
Unquoted identifiers are case-insensitive; "Quoted Names" keep spaces/case (prefer snake_case).
Required functions include SUM, COUNT, COUNT(DISTINCT), AVG, MIN, MAX, MEDIAN, STDDEV*,
VARIANCE*, PERCENTILE_CONT/DISC, DATE_TRUNC, DATEADD(part, n, d), DATEDIFF(part, a, b),
EXTRACT/DATE_PART, CURRENT_DATE/TIMESTAMP, CONCAT/||, LENGTH, SUBSTRING, LOWER/UPPER, TRIM,
REPLACE, SPLIT_PART, CONTAINS, STARTSWITH/ENDSWITH, REGEXP_LIKE, COALESCE/NULLIF/IFF/IF, CASE,
CAST/TRY_CAST, GREATEST/LEAST, ROW_NUMBER/RANK/DENSE_RANK/NTILE/LAG/LEAD/FIRST_VALUE.
Dialects (free strings, registered upper-case tokens): OSSIE_SQL_2026, ANSI_SQL, SNOWFLAKE,
DATABRICKS, BIGQUERY, TABLEAU ("Tableau calculations"), DAX, MDX, MAQL, SIGMA, THOUGHTSPOT,
HOLISTICS_AQL. `sql` wins over an OSSIE_SQL_2026 dialect entry; other dialects are opaque and
may be ignored by consumers, so always give a portable `sql` when you can.""",
    "tableau": """\
# Tableau -> Ossie (what export_to_ossie does)
- Published data source -> model; logical tables -> datasets; column fields -> fields (sql =
  the source column); role dimension -> `dimension`; datatype mapped; descriptions and
  semantic-context statements -> description / ai_context.instructions.
- Row-level calcs -> fields; aggregate calcs (or calcs built on one) -> metrics, dataset-scoped
  when they read one logical table, model-scoped (qualified names) otherwise. Every calc keeps
  its formula in the TABLEAU dialect; portable `sql` is added when the translation is exact.
- Measures with a Tableau default aggregation also become metrics (total_sales = SUM(sales)).
- Translated: SUM AVG MIN MAX COUNT COUNTD->COUNT(DISTINCT) MEDIAN STDEV(P) VAR(P), arithmetic,
  comparisons, IF/ELSEIF/ELSE/END and IIF -> CASE, CASE, ZN/IFNULL -> COALESCE, ISNULL -> IS NULL,
  DATEADD/DATEDIFF/DATETRUNC/DATEPART, TODAY()/NOW(), DATE/INT/FLOAT/STR -> CAST, LEN/MID/
  CEILING renamed, {FIXED [d]: AGG(x)} -> AGG(x) OVER (PARTITION BY d) in row-level fields.
- Refused (TABLEAU dialect only, reported as a loss): INCLUDE/EXCLUDE LODs, table calcs
  (WINDOW_*, RUNNING_*, RANK, INDEX, LOOKUP, TOTAL...), ATTR, parameters, string `+`
  concatenation, unknown functions (e.g. MAKEDATE), LODs inside aggregates.
- Not in Tableau Knowledge, so not exported: physical table paths (`source` is the logical table
  name: replace it), join keys (add `relationships` by hand), parameters, sets, groups, bins,
  hierarchies, folders, format strings, row-level security.
- Tableau ids, captions, roles, default aggregation, hidden flags, original formulas and the
  semantic contexts are kept in custom_extensions vendor TABLEAU, so import can map back exactly.
Caveat: a FIXED LOD ignores the view's dimension filters; the window form is computed over all
rows of the dataset. Same result at row level, but check filter behaviour in the target tool.""",
    "relationships": """\
# Relationships
A relationship is a many(from) -> one(to) equi-join: from_columns[i] = to_columns[i]. There is
no cardinality, join type, non-equi predicate or active/inactive flag; many-to-many cannot be
expressed (Power BI's converter reports it as "no equivalent"). The validator warns when
to_columns do not cover a key of the `to` dataset. Fan-out-free measure semantics (Layer 2,
relational_semantics.md, draft) only expose dataset-scoped metrics, another reason to prefer
them. Tableau relationships (noodles) carry cardinality and referential-integrity settings that
Ossie cannot hold: note them in ai_context or custom_extensions.""",
    "ai_context": """\
# AI context
Allowed on the model, datasets, fields, metrics and relationships. Either a string, or an object
{instructions: str, synonyms: [str], examples: [str] (sample questions)} with extra keys allowed.
import_from_ossie turns it into Tableau Knowledge semantic-context statements: the description,
the instructions, "Also known as: ..." for synonyms, "Example question: ..." for examples, the
metric definition, and "Relates to ..." for relationships. Converters differ in what they keep:
Snowflake drops relationship ai_context; Wisdom drops synonyms and field/metric ai_context;
Power BI stores the whole object in an annotation. There are no verified queries yet.""",
    "extensions": """\
# custom_extensions
[{vendor_name, data}] on the model, datasets, fields, metrics and relationships. `data` is an
opaque string (conventionally JSON); apache/ossie PR #522 may turn it into an object, so read
both. Converter rules: apply only your own vendor's entries on export, put everything unmapped
into your vendor's entry on import, preserve other vendors' entries and field order, and never
discard silently. This server uses vendor TABLEAU (not SALESFORCE: the Salesforce converter
copies every SALESFORCE key into Tableau Next payloads).""",
    "validation": """\
# Validation
validate_ossie checks a document against the bundled official JSON Schema (0.2.0.dev0, draft
2020-12) plus the spec's semantic rules: duplicate dataset/field/metric/relationship names, keys
and relationship columns that are not declared fields, unknown from/to datasets, column-count
mismatches, qualified names in dataset-scoped metrics. 0.1.1 documents are converted first.
The reference validator is apache/ossie validation/validate.py (needs jsonschema, pyyaml,
sqlglot; also parses SQL dialects). Minimal valid document:
  version: "0.2.0.dev0"
  name: m
  datasets: [{name: d, source: db.s.t}]
Round-trip test ladder: compliance/portability M0-M3 (single SUM metric, scalar field,
SUM/COUNT/COUNT DISTINCT/AVG/MIN/MAX, conditional SUM) with grader outcomes PORTABLE, REWRITTEN,
SURVIVED_VENDOR_ONLY, DROPPED_DIAGNOSED, DROPPED_SILENT, REJECTED.""",
    "converters": f"""\
# Converters to other platforms (apache/ossie-converters @ {CONVERTERS_COMMIT[:7]})
None are published: run them from pinned git commits with uv (needs git and network on first
use). These are third-party, pre-release code running with your privileges: read them first.
Commands are taken from each converter's README/pyproject and were NOT run by this server.
  C=git+https://github.com/apache/ossie-converters@{CONVERTERS_COMMIT}
  OSSIE="apache-ossie @ git+https://github.com/apache/ossie@{OSSIE_PYTHON_COMMIT}#subdirectory=python"
- Snowflake (Ossie -> Cortex Analyst semantic-model YAML, export only; needs db.schema.table
  sources; drops relationship ai_context):
    uvx --from "$C#subdirectory=snowflake" ossie-snowflake -i model.ossie.yaml -o model.cortex.yaml
- dbt MetricFlow (both ways; output is a compiled semantic_manifest.json, not authoring YAML):
    uvx --from "$C#subdirectory=dbt" --with "$OSSIE" ossie-dbt ossie-to-msi -i model.ossie.yaml -o semantic_manifest.json
- Power BI / Fabric (TMSL model.bim, both ways; untranslatable SQL becomes an annotated BLANK()):
    uvx --from "$C#subdirectory=microsoft" ossie-microsoft export -i model.ossie.yaml -o model.bim
- Databricks metric views (Java 21 + Maven; build the jar, then):
    java -jar ossie-databricks-converter-0.1.0-SNAPSHOT.jar export model.ossie.yaml -o metric_view.yaml
- Tableau Next / Data 360 semantic models (Java 21; prefers the TABLEAU dialect, emits Tua; only
  SUM/AVG/MIN/MAX/COUNT/COUNTD/arithmetic/CASE/COALESCE): java -jar ossie-salesforce-converter-
  0.1.0-SNAPSHOT.jar toSF model.ossie.yaml
- Also both ways: cube, thoughtspot, omni, sigma, gooddata, holistics, honeydew, orionbelt.
  Export only: hex. In review: LookML, Lightdash, SAP CSN, and a classic Tableau .tds/.twb
  converter (ossie-converters PR #82, Snowflake/BigQuery sources).
Converters keep their own vendor's stash and need a portable `sql` (or ANSI_SQL) to carry a
metric across vendors: TABLEAU/DAX/Tua/TML-only expressions do not travel.""",
    "porting": """\
# Porting a Tableau semantic layer, step by step
1. list_sources -> pick the published data source; graph_health shows how much business context
   it carries (context_coverage). Add missing definitions first: they travel as ai_context.
2. export_to_ossie(datasource) -> save `ossie` as the returned file_name.
3. Read `losses`. Fix in the YAML: set each dataset `source` to database.schema.table, add
   `relationships` with key fields, hand-translate calcs left in the TABLEAU dialect (add `sql`).
4. validate_ossie(document) until valid with no errors.
5. Run the target converter (ossie_guide topic `converters`), then load into the target tool.
6. Going the other way (a model authored elsewhere, or edited YAML): import_from_ossie(document,
   target_datasource_id) to preview which semantic contexts it would write; review; then
   apply=true. Elements match by exported ids first, then names; unmatched ones are listed.
Pitfalls: Ossie has no default aggregation, display names, hierarchies, parameters, sets,
non-additive metrics, RLS or join types; Salesforce reads DateTime as DateTimeTz; Power BI maps
Time/DateTimeTz to dateTime; Snowflake infers metric types; dbt drops window, cumulative and
conversion metrics; prefer dataset-scoped metrics.""",
}

TOPICS = list(GUIDE)
