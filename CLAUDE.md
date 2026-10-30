# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

An MCP server (stdio transport) that exposes the Tableau Knowledge Graph API (`/api/v1/knowledge`, Tableau Cloud only) as sixteen tools: nine reads, three semantic-context writes, and four Apache Ossie (formerly OSI) tools for porting semantic layers, plus two MCP prompts. Runtime dependencies: the `mcp` SDK and PyYAML (Ossie YAML). Everything else is stdlib (`urllib` for HTTP); keep it that way. Sister project: `../tableau-graphql-mcp` (Metadata API), which this one mirrors in structure and quality bar.

## Commands

```bash
uv sync --all-extras                                 # install package + dev tools
uv run pytest                                        # all tests (offline; no Tableau needed)
uv run pytest tests/test_server_tools.py::test_name  # single test
uv run pytest --cov=biztory_tableau_knowledge_mcp    # with coverage (CI floor: 85%, branch coverage)
uv run ruff check . && uv run ruff format --check .  # lint + format
uv run mypy                                          # strict mode, src/ only
uv run --python 3.12 --group fuzz python fuzz/fuzz_untrusted_input.py -max_total_time=60   # Linux only
npx @modelcontextprotocol/inspector uv run biztory-tableau-knowledge-mcp   # interactive tool testing
```

Tests marked `live` hit a real site and only run with `TABLEAU_LIVE` + credentials; `TABLEAU_LIVE_WRITE` additionally runs a create/update/delete round trip on a context the test creates itself. Everything else goes through the real client against the in-process fake API in `tests/conftest.py` (patches `client._OPENER.open`).

## Architecture

MCP client ⇄ stdio ⇄ `server.py` (FastMCP tools) → `client.py` (auth + HTTP) → `/api/v1/knowledge`.

- `server.py`: the 12 `@mcp.tool()` functions. Docstrings are the model-facing API docs and carry the API's quirks. `_resolve()` turns a node id OR a description into a node: exact `GET /nodes/{id}` first, on `not_found` the top semantic-search match, returning a `resolution` block (score + alternatives). Write tools never use `_resolve`: exact ids only.
- `client.py`: `TableauClient`. PAT sign-in (REST version from the *versioned* `/api/3.4/serverinfo`; the bare `/api/serverinfo` is 404 on Cloud) → `X-Tableau-Auth`, re-sign-in once on 401. `knowledge(method, path, query, body)` is the one call path. `describe_http_error()` maps the error envelope (`code`/`message`/`trace_id`) to a message plus a next step (`_HINTS`); `TableauError.code` carries the API code (`_resolve` branches on `not_found`).
- `config.py`: `Settings.from_env()`; all config via `TABLEAU_*` env vars. `GRAPH_ID_PATTERN` is the documented `graph_id` pattern.
- `ossie.py`: Tableau Knowledge <-> Apache Ossie. `load_datasource` (PDS -> logical tables -> fields, plus contexts), `export_datasource` (the mapping; every gap goes in `losses`), `validate_document` (bundled `schemas/ossie-schema-0.2.0.dev0.json` + semantic rules), `normalize` (0.1.1 -> 0.2.0 shape), `plan_import`/`apply_plan`. Tableau ids round-trip in `custom_extensions` vendor `TABLEAU`.
- `tableau_calc.py`: Tableau formula -> OSSIE_SQL translator. Conservative by design: anything not exact raises `Untranslatable` with a reason, and the formula stays in the `TABLEAU` dialect.
- `ossie_guide.py`: the reference text served by `ossie_guide`, with the spec/converter commits it was read at. Update it (and the bundled schema) when the spec moves.

## Non-negotiable constraints

- **stdout is the JSON-RPC channel.** Never print to stdout; log to stderr.
- **Writes stay narrow.** Only semantic contexts; exact ids; validated before sending (statements 5-1000 chars, exactly one of target/global); honest `ToolAnnotations` (`_READ`, `_CREATE`, `_REPLACE`). Never auto-resolve a phrase for a write.
- **Ids go through `segment()`** (one encoded path segment). `graph_id` is pattern-checked before sending.
- **Secrets from env only**: never tool arguments, never logged, never in output or errors.
- **Keep the tool surface small.** Improve a docstring before adding a tool.
- **The tool contract is pinned.** `tests/test_tool_contract.py` snapshots names + args in `tests/tool_contract.json`; regenerate on purpose (command in its docstring), note it in `CHANGELOG.md`, MAJOR bump for renames/removals.
- **Node-scoped writes may be refused.** Tableau Cloud can answer `node_semantic_context_disabled` (only global context accepted through the API). Import handles it with `global_fallback`; any new write path must too.
- **Ossie output must validate.** `export_datasource` validates its own model; a mapping change that produces an invalid model is a bug. Never drop information silently: put it in `losses` or the `TABLEAU` stash.
- **Bound big responses** and say so (e.g. `graph_health` limit 20 with full totals). Never truncate silently.
- Behavior changes need a test and a `CHANGELOG.md` entry under `[Unreleased]`.

## Versioning

Version lives in `src/biztory_tableau_knowledge_mcp/__init__.py` (hatch reads it). Also update `server.json` on release.
