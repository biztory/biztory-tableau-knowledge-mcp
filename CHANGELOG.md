# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/) and this project adheres to
[Semantic Versioning](https://semver.org/). Note that the **tool schema is the public
API**: renaming a tool or a required argument is a MAJOR change.

## [Unreleased]

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
