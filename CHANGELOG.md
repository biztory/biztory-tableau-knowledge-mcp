# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/) and this project adheres to
[Semantic Versioning](https://semver.org/). Note that the **tool schema is the public
API**: renaming a tool or a required argument is a MAJOR change.

## [Unreleased]

### Added
- Releases are listed on the official MCP registry automatically: `mcp-registry.yml` runs after the
  PyPI publish and logs in with GitHub OIDC, which proves the repo belongs to the `biztory` org.
  The `mcp-publisher` binary is pinned to v1.7.9 and checked against its published SHA-256.

### Changed
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
