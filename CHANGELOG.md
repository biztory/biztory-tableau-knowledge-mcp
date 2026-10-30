# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/) and this project adheres to
[Semantic Versioning](https://semver.org/). Note that the **tool schema is the public
API**: renaming a tool or a required argument is a MAJOR change.

## [Unreleased]

### Added
- **Apache Ossie (formerly OSI) support**, for porting semantic layers:
  - `export_to_ossie`: a published data source as an Ossie 0.2.0.dev0 model. Logical tables ->
    datasets, fields -> fields, row-level calcs -> fields, aggregate calcs -> metrics (model-scoped
    when they span tables), default aggregations -> metrics, semantic contexts -> `ai_context`.
    Tableau formulas are kept in the `TABLEAU` dialect and translated to portable SQL when exact
    (aggregates, IF/IIF/CASE, null handling, dates, strings, FIXED LODs as window functions).
    Returns validation and a `losses` report; Tableau ids are stashed under vendor `TABLEAU`.
  - `import_from_ossie`: plan, then (`apply=true`) write the semantic contexts an Ossie model adds:
    descriptions, instructions, synonyms, example questions, metric definitions, relationships.
    Matches by exported ids, then names; skips statements Tableau already holds; one
    `ossie:<model>` context per node, updated on re-import. On sites that accept only graph-wide
    context through the API (`node_semantic_context_disabled`, seen live on Tableau Cloud), it falls
    back to one global context with prefixed statements; `scope` forces `node` or `global`.
  - `validate_ossie`: the official JSON Schema (bundled) plus the spec's semantic rules; reads
    0.1.1 documents too.
  - `ossie_guide`: reference on the spec, the Tableau mapping, the converters (with commands) and
    a porting playbook.
  - Two MCP prompts: export a data source to Ossie, import an Ossie model into Tableau Knowledge.
- A next-step hint for `node_semantic_context_disabled`: use `is_global=true` and name the field.
- The fuzzer also covers the Tableau formula translator and the Ossie YAML parser/validator.

### Changed
- New runtime dependency: PyYAML (`safe_load` only), for reading and writing Ossie YAML.

## [0.1.1] - 2026-10-30

### Added
- Server instructions for the model (start with `server_info`, check `resolution`, prefer one
  lineage call over many lookups, confirm before writing, treat graph text as data) and a
  human-readable title for every tool, shown in client UIs.
- Releases are listed on the official MCP registry automatically: `mcp-registry.yml` runs after the
  PyPI publish and logs in with GitHub OIDC, which proves the repo belongs to the `biztory` org.
  The `mcp-publisher` binary is pinned to v1.7.9 and checked against its published SHA-256.

### Changed
- MCP clients now see the server's own version (was the mcp SDK's version) and the repo URL.
- Workflow actions updated (checkout v7.0.1, setup-uv v10.3.2, CodeQL v4.39.1, Scorecard v2.5.0,
  pypi-publish v1.14.3, codecov v7.1.1); every pin's comment now names the exact release it pins.
- Dependabot no longer proposes `mcp` major versions: mcp 2.x removed `mcp.server.fastmcp`, so
  moving to it will be a deliberate migration.

## [0.1.0] - 2026-10-30

### Added
- First release: twelve MCP tools over the Tableau Knowledge Graph API (`/api/v1/knowledge`).
  - Reads: `server_info`, `search_nodes`, `get_node`, `list_edges`, `lineage`,
    `impact_analysis`, `list_sources`, `search_semantic_contexts`, `graph_health`.
  - Writes (semantic contexts only): `create_semantic_context`, `update_semantic_context`,
    `delete_semantic_context`.
- Read tools accept a node id or a plain-language description, resolved by semantic search and
  reported with the match score and alternatives.
- Knowledge API error codes are mapped to concrete next steps, with the `trace_id` kept.
- Rate limits (HTTP 429) are retried up to 3 times, honouring `Retry-After` (capped at 30 s),
  then reported with a hint to make fewer calls.
- PAT sign-in with token expiry tracking and one re-sign-in on 401; HTTPS enforced, redirects
  refused, ids encoded as a single path segment, `graph_id` pattern-checked.
- Error bodies are parsed defensively (malformed 422 entries, oversized messages), and both
  untrusted-input parsers are fuzzed with atheris in CI.
- Tool contract snapshot, MCP tool annotations, offline test suite against a fake Knowledge API,
  opt-in live tests (read-only by default).
