# Contributing

Thanks for your interest! This is a small, focused MCP server; contributions that keep it
that way are very welcome.

## Development setup

```bash
git clone https://github.com/biztory/biztory-tableau-knowledge-mcp && cd biztory-tableau-knowledge-mcp
uv sync --all-extras          # installs the package + dev tools
uv run pytest                 # tests run offline, no Tableau needed
uv run ruff check .
uv run ruff format .
```

Run the server from source, pointed at your own Tableau site:

```bash
export TABLEAU_SERVER=https://<pod>.online.tableau.com
export TABLEAU_SITE_CONTENT_URL=YourSite
export TABLEAU_PAT_NAME=... TABLEAU_PAT_SECRET=...
uv run biztory-tableau-knowledge-mcp
# or exercise it interactively:
npx @modelcontextprotocol/inspector uv run biztory-tableau-knowledge-mcp
```

## Guidelines

- **Keep the tool surface small.** Every tool is tokens in the model's context. Prefer
  improving a tool's docstring over adding a narrow tool.
- **Writes stay narrow.** Only semantic-context create/update/delete. Write tools take exact ids,
  never a search phrase, and carry honest MCP annotations.
- **Stdlib first.** Runtime dependencies are the MCP SDK and PyYAML (for Ossie YAML); keep it
  that way unless there's a strong reason.
- **Secrets via env only**, never as tool arguments, never logged.
- **No writes to stdout**: it carries the JSON-RPC protocol. Log to stderr.
- Add/adjust a test for behavior changes; update `CHANGELOG.md` under `[Unreleased]`.
- A tool rename or required-arg change is a **MAJOR** version bump; call it out in the PR.

## Reporting issues

Open an issue with your Tableau Cloud release, the `trace_id` from the error if there is one,
and the exact tool call and (sanitized) response.
