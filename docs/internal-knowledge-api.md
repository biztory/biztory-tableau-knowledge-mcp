# Tableau Knowledge internal API (`/knowledge/api`)

Scanned 2026-11-04 from the Tableau Knowledge web app bundle (`/knowledge/assets/index-DFD3m6fD.js`,
downloadable without login) on 10ax / biztorypulse. **Undocumented**: this is the API the web app
calls, not the public `/api/v1/knowledge` this MCP server uses. It can change without notice.

**Auth:** a PAT sign-in token in `X-Tableau-Auth` works (no browser, no cookie). Verified for every
GET below and for `POST tableau-cloud/ingest-async`. Writes other than ingest were NOT tested.
Graph-scoped calls take `?graph_id=<id>`.

## Verified with the PAT (GET, all 200)

| Path | Returns | In the public API? |
|---|---|---|
| `config` | feature flags: `node_semantic_context_enabled`, `admin_enabled`, `max_graphs_per_site`, `auto_refresh_enabled` | no |
| `tenant-config` | `max_graphs` | no |
| `auth/me` | email, user_luid, username, orgs | no |
| `connections` | Tableau Cloud connections of the tenant | no |
| `graphs` (`?include_stats=true`) | graphs (+stats) | yes (`/graphs`) |
| `graphs/{id}` | one graph | partly |
| `graph/stats` | total_nodes, total_relationships, connected_sources, workbooks | no |
| `graph/coverage` | `context_coverage` (0-1) | no (inside suggestions) |
| `graph` | the whole graph, nodes + edges (1 MB here) | no |
| `nodes` (`?type=PDS`) | nodes by type | via POST search |
| `nodes/search?q=&limit=` | semantic search | yes (POST) |
| `nodes/{id}` | node + `semantic_context` | yes |
| `edges` (`?type=`) | all edges | via POST search |
| `suggestions` | graph health | yes (POST) |
| `semantic-context` (`?is_global=`, `?target_node_id=`) | contexts | yes |
| `pds` | PDS nodes in the graph | via sources search |
| `tableau-cloud/projects` | site projects | no |
| `tableau-cloud/datasources?page=&page_size=&search=&project=&sort=` | **site** data sources (not just the graph's), with search | no |
| `tableau-cloud/workbooks?…` | site workbooks, same filters | no |
| `tableau-cloud/ingest-async/by-graph/{id}` | ingest status + per-asset result | no |

## Writes found in the UI (only ingest tested)

| Method + path | Body | Effect |
|---|---|---|
| `POST tableau-cloud/ingest-async?graph_id=` | `{"targets":[{"asset_type":"pds"\|"workbook","luids":[…]}]}` | **adds** sources to a graph. Tested: 202, additive. Empty `luids` = everything: never send. |
| `POST graphs/{id}/remove-nodes` | node selection | removes sources from a graph |
| `POST graphs` / `PATCH graphs/{id}` / `DELETE graphs/{id}` | name, description | graph create / rename / delete |
| `POST graphs/{id}/activate` | none | set the primary graph |
| `POST/PATCH/DELETE semantic-context[/{id}]` | as public API | same as public |
| `POST semantic-context/document?graph_id=` | multipart file (.txt .md .csv .pdf .docx .xlsx, max 2 MB) | builds semantic context from a document |
| `POST chat?graph_id=` | `{question, conversation_id, message_history}` (streamed) | the Tableau Agent chat |
| `POST connections/tableau-cloud`, `…/validate` | connection config | create a Tableau Cloud connection |

## Admin (404 here: `config.admin_enabled` is false)

`admin/db/tables`, `admin/db/tables/{t}/rows`, `admin/db/query`, `admin/db/write` (raw SQL),
`admin/db/nl-query`, `admin/permissions/{session,plane,user,objects}`, `admin/semantic-search[/graphs]`,
`admin/semantic-subgraph`. Off on this tenant; do not build on them.

## Notes

- `config.node_semantic_context_enabled: false` is the flag behind the public API's
  `node_semantic_context_disabled` error. A tool can read it up front instead of failing a write.
- The UI sends an `X-XSRF-TOKEN` for cookie auth; PAT calls did not need it.
