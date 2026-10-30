<!-- mcp-name: io.github.biztory/biztory-tableau-knowledge-mcp -->

<h1 align="center">biztory-tableau-knowledge-mcp</h1>

<p align="center"><em>Search, explore and enrich your Tableau Knowledge graph, in any MCP client.</em></p>

<p align="center">
  <a href="https://github.com/biztory/biztory-tableau-knowledge-mcp/actions/workflows/ci.yml"><img src="https://github.com/biztory/biztory-tableau-knowledge-mcp/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="https://github.com/biztory/biztory-tableau-knowledge-mcp/actions/workflows/codeql.yml"><img src="https://github.com/biztory/biztory-tableau-knowledge-mcp/actions/workflows/codeql.yml/badge.svg" alt="CodeQL"></a>
  <a href="https://github.com/astral-sh/ruff"><img src="https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json" alt="Ruff"></a>
  <img src="https://img.shields.io/badge/python-3.10%20to%203.13-3776AB.svg" alt="Python versions">
  <img src="https://img.shields.io/badge/MCP-compatible-8A2BE2.svg" alt="MCP compatible">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-00696E.svg" alt="MIT License"></a>
  <img src="https://img.shields.io/badge/Tableau-Cloud%20%2B%20Knowledge-006EFF.svg" alt="Tableau Cloud">
  <img src="https://img.shields.io/badge/deps-just%20mcp-64FA8C.svg" alt="Minimal dependencies">
</p>

---

**biztory-tableau-knowledge-mcp** turns your site's [Tableau Knowledge Graph API](https://help.tableau.com/current/api/rest_api/en-us/REST/rest_api_ref_tableau_knowledge_graph.htm) into twelve MCP tools, so an AI assistant (Claude, Cursor, Cline, and others) can work with the graph in plain language:

- _"Which fields mean revenue, and where do they come from?"_
- _"If the **Superstore** data source changes, which workbooks are affected?"_
- _"What business context is attached to **Profit**? Add: 'Profit is after returns, in EUR.'"_
- _"How healthy is the graph, and what are the top fixes?"_

### Why it's different
- **Ask by meaning, not by id.** Every read tool takes a node id *or* a description ("the net sales field"). Descriptions resolve through the graph's semantic search, and the answer says which node it picked, with the match score and runners-up, so a wrong guess is visible.
- **Answers, not raw payloads.** `impact_analysis` groups and counts affected assets by type; `lineage` adds node and edge counts; `graph_health` caps the suggestion list while keeping totals.
- **Errors tell the model what to do next.** Tableau's error codes become concrete steps: `site_not_entitled` → an admin must enable Knowledge; `no_active_graph` → pass a `graph_id`; no embedder → use exact ids instead of search. Each error keeps its `trace_id` for support.
- **Writes are narrow and labelled.** Only semantic contexts can be created, updated or deleted. Write tools take exact ids only (a fuzzy match never decides where a write lands), validate before sending, carry honest MCP annotations so clients can ask first, and `delete_semantic_context` returns what it deleted.
- **Tiny and safe.** Stdio only (no inbound port), secrets from env only, HTTPS enforced, redirects refused, ids encoded so they can't reach another endpoint, and **no dependencies beyond the MCP SDK** (stdlib `urllib` for HTTP).

## Requirements

Tableau Knowledge is a **Tableau Cloud** feature (September 2026 and later, Tableau+ license). It is not available on Tableau Server. Any user can read the graph; nodes are filtered by your content permissions. Writing semantic contexts needs the **Creator** role.

## Quickstart

You need [`uv`](https://docs.astral.sh/uv/) (`curl -LsSf https://astral.sh/uv/install.sh | sh`) and a Tableau [Personal Access Token](https://help.tableau.com/current/online/en-us/security_personal_access_tokens.htm).

**Claude Code, from a clone:**

```bash
claude mcp add tableau-knowledge \
  -e TABLEAU_SERVER=https://10ax.online.tableau.com \
  -e TABLEAU_SITE_CONTENT_URL=YourSite \
  -e TABLEAU_PAT_NAME=my-token \
  -e TABLEAU_PAT_SECRET=the-full-secret \
  -- uv run --directory /path/to/biztory-tableau-knowledge-mcp biztory-tableau-knowledge-mcp
```

Once published to PyPI, the last line becomes `-- uvx biztory-tableau-knowledge-mcp`.

## Configuration

All configuration is via environment variables (set them in your client's `env` block, never on the command line).

| Env var | Required | Default | Description |
|---|:---:|---|---|
| `TABLEAU_SERVER` | yes | n/a | `https://<pod>.online.tableau.com`. Any URL from your browser works; the path is stripped. |
| `TABLEAU_SITE_CONTENT_URL` | yes | `""` | Site slug (the part after `/#/site/`). |
| `TABLEAU_PAT_NAME` | yes¹ | n/a | Personal Access Token **name**. |
| `TABLEAU_PAT_SECRET` | yes¹ | n/a | PAT **secret**: the whole string, do **not** split on `:`. |
| `TABLEAU_KNOWLEDGE_GRAPH_ID` | no | primary graph | Default graph for every tool. `server_info` lists the site's graphs; each tool also takes a `graph_id`. |
| `TABLEAU_TIMEOUT` | no | `60` | Per-request timeout (seconds). |
| `TABLEAU_API_VERSION` | no | auto | REST version used for sign-in; else read from serverinfo. (The Knowledge API itself is always `v1`.) |
| `TABLEAU_ALLOW_HTTP` | no | off | Set to `1` to allow a plain `http://` server on a trusted network. Otherwise only HTTPS (or loopback). |
| `TABLEAU_LOG_LEVEL` | no | `INFO` | `DEBUG` logs each HTTP call (method + URL; never headers, bodies or secrets) to stderr. |
| `TABLEAU_AUTH_TOKEN` | no | n/a | Advanced: a pre-obtained `X-Tableau-Auth` session token instead of a PAT. |

¹ Provide a PAT (`TABLEAU_PAT_NAME` + `TABLEAU_PAT_SECRET`) **or** `TABLEAU_AUTH_TOKEN`.

<details>
<summary><b>Claude Desktop</b> · <b>Cursor</b> · <b>Cline</b> · <b>Windsurf</b></summary>

Every client uses the same `mcpServers` schema. Claude Desktop: `claude_desktop_config.json` (macOS: `~/Library/Application Support/Claude/`, Windows: `%APPDATA%\Claude\`). Cursor: `~/.cursor/mcp.json`. Cline: `cline_mcp_settings.json`. Windsurf: `~/.codeium/windsurf/mcp_config.json`.

```json
{
  "mcpServers": {
    "tableau-knowledge": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/biztory-tableau-knowledge-mcp", "biztory-tableau-knowledge-mcp"],
      "env": {
        "TABLEAU_SERVER": "https://10ax.online.tableau.com",
        "TABLEAU_SITE_CONTENT_URL": "YourSite",
        "TABLEAU_PAT_NAME": "my-token",
        "TABLEAU_PAT_SECRET": "the-full-secret"
      }
    }
  }
}
```

Fully **quit and reopen** the client, then check its tools menu.
</details>

## Tools

`node` arguments accept a node id **or** a plain-language description. Every graph-scoped tool also takes an optional `graph_id`.

| Tool | What it does | Key args |
|---|---|---|
| `server_info` | Site, versions, auth, whether Knowledge is enabled, and every graph (id, name, status, primary). Good first call. | none |
| `search_nodes` | Semantic search over the graph ("revenue" finds "Net Sales"), ranked with scores. | `query`, `node_type`, `scope_id`, `limit`, `threshold` |
| `get_node` | One node: properties, attached semantic statements, child nodes. | `node`, `include_children` |
| `list_edges` | The edges around one node, with direction and the node on the other end. | `node`, `edge_type`, `direction` |
| `lineage` | Upstream and downstream neighborhood (10 steps) along lineage/dependency edges, with counts. | `node` |
| `impact_analysis` | Everything that depends on a node, transitively, grouped and counted by type. | `node` |
| `list_sources` | The published data sources and workbooks the graph is built from. | `node_type` |
| `search_semantic_contexts` | Business statements on the graph: all, graph-wide only, or attached to one node. | `node`, `is_global` |
| `graph_health` | Health score, coverage metrics, and prioritized improvement suggestions. | `pds_id`, `severity`, `suggestion_type`, `limit` |
| `create_semantic_context` ✏️ | Attach statements to one node (exact id) or to the whole graph. | `statements`, `target_node_id` / `is_global`, `name` |
| `update_semantic_context` ✏️ | Replace statements, rename, re-attach or make graph-wide. | `ctx_id`, `statements`, `name`, `attach_to` / `make_global` |
| `delete_semantic_context` ✏️ | Delete a user-managed context; returns what was deleted. | `ctx_id` |

✏️ = writes. Only semantic contexts can be written; Tableau-managed (`SEMANTIC_CONTEXT_EXTERNAL`) contexts are read-only.

## Example prompts

- _"Use server_info: which graphs does this site have, and which is primary?"_
- _"Find the fields that mean revenue, then show the lineage of the best match."_
- _"Run impact_analysis on the Superstore data source: which workbooks and dashboards depend on it?"_
- _"What semantic context is attached to the Profit field?"_
- _"Show the graph's health score and the five high-severity suggestions."_
- _"Add a statement to the Profit field: 'Profit is calculated after returns and reported in EUR.'"_

## Things the API does that you should know

- **Search is by meaning.** Scores are relative; a top score around 0.4-0.5 can still be the right node, but check the alternatives.
- **Lineage follows dependencies, not containment.** A single field's lineage is often just itself; `list_edges` shows which table HAS it.
- **Context search doesn't filter by text yet.** `search_semantic_contexts` filters by node or `is_global` only; find statements by meaning with `search_nodes`.
- **Lookups differ on unknown ids.** `get_node` and `impact_analysis` say "not found"; the API's lineage returns an empty graph. This server resolves the node first, so you always get a clear error.
- **Deletes are idempotent.** Deleting an unknown id succeeds without effect.

## Security

- **Writes are limited to semantic contexts**, take exact ids, are validated before sending, and are annotated (`readOnlyHint`, `destructiveHint`, `idempotentHint`) so your client can ask before each one. Use a Viewer/Explorer PAT to make the server read-only in practice.
- **Local and stdio-only.** No inbound network port is opened.
- **Secrets from env only.** Never tool arguments, never logged, never returned.
- **Credentials go only to your Tableau host.** HTTPS required, certificates verified, redirects refused.
- **No injection through ids.** Node and context ids are encoded as one path segment; `graph_id` is checked against Tableau's pattern before it is sent.

See [SECURITY.md](SECURITY.md) for the threat model, data handling and disclosure policy.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Sign-in fails (401, `401001`) | The PAT is invalid or expired (15 days unused). Create a new one and check `TABLEAU_SITE_CONTENT_URL`. |
| `site_not_entitled` | The site doesn't have Tableau Knowledge; a Tableau Cloud Manager admin must enable it. |
| `no_active_graph` | The site has no primary graph: pass a `graph_id` from `server_info`. |
| "Semantic search is unavailable" | The graph has no embedder. Use exact ids (`list_sources` → `get_node`). |
| `tableau_forbidden` on a write | Writing semantic contexts needs the Creator role. |

Inspect the server directly with the MCP Inspector:

```bash
npx @modelcontextprotocol/inspector uv run biztory-tableau-knowledge-mcp
```

## Development

```bash
uv sync --all-extras
uv run biztory-tableau-knowledge-mcp                   # run from source (needs the env vars above)
uv run pytest --cov=biztory_tableau_knowledge_mcp      # tests + coverage (offline; no Tableau needed)
uv run ruff check . && uv run ruff format --check . && uv run mypy
```

Tool tests run through the real client against an in-process fake of the Knowledge API (`tests/conftest.py`). Live tests are opt-in: `TABLEAU_LIVE=1` runs the read-only ones; `TABLEAU_LIVE_WRITE=1` adds a round trip that creates, updates and deletes one temporary context it made itself.

The same gates run in CI (Linux/macOS/Windows, Python 3.10 to 3.13), plus CodeQL and the OpenSSF Scorecard.

## Roadmap

- [ ] Publish to PyPI and the MCP registry
- [ ] Import an OSI (Open Semantic Interchange) model as semantic contexts

## License

[MIT](LICENSE) © [Biztory](https://www.biztory.com).
