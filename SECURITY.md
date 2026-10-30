# Security policy

## Supported versions

| Version | Supported |
|---|---|
| 0.1.x | Yes |

## Reporting a vulnerability

Report privately through GitHub:
**[Report a vulnerability](https://github.com/biztory/biztory-tableau-knowledge-mcp/security/advisories/new)**.
If you cannot use GitHub, email **tim.dries@biztory.be**. Please do not open a public issue.

What to expect:

- Acknowledgement within **3 business days**, and a first assessment within **10 business days**.
- A fix within **30 days** for high or critical issues, and within **90 days** otherwise.
- Coordinated disclosure through a GitHub Security Advisory, with a CVE where warranted.
  Reporters are credited unless they ask not to be.

## What the server guarantees

- **Writes are narrow and labelled.** Nine tools only read. Three tools write, and only to
  *semantic contexts* (the descriptive statements attached to graph nodes): create, update and
  delete. They cannot change workbooks, data sources, permissions or any other content, and
  Tableau-managed contexts are refused by the API. Every tool carries MCP annotations
  (`readOnlyHint`, `destructiveHint`, `idempotentHint`) so your client can ask before a write.
  Write tools take exact node and context ids only, never a search phrase, so a fuzzy match can
  never decide where a write lands. There is no SQL, file or shell access.
- **Local and stdio-only.** It runs as a child process of your MCP client and opens **no
  inbound network port**.
- **Credentials from the environment only.** The PAT or token are never tool
  arguments, never logged and never returned in tool output.
- **Credentials go only to your Tableau host.**
  - HTTPS is required. Plain `http://` is refused unless the host is loopback or you set
    `TABLEAU_ALLOW_HTTP=1`.
  - TLS certificates are always verified against the system trust store.
  - HTTP redirects are refused, so a redirect can never carry your session token to another
    host.
- **Least privilege.** Results are limited to what the PAT's Tableau identity may see.
- **No telemetry.** The server contacts nothing except `TABLEAU_SERVER`.

## Data handling

- **Flow.** Tableau Knowledge API, then this server, in memory only, then your MCP client.
  Your client sends tool results to the language model provider it uses. Check that this is
  allowed under your organisation's data policies.
- **Personal data.** Results include Tableau content names and node properties as the Knowledge
  API returns them, and a semantic context's `updated_by` identity. Nothing else about people is
  requested.
- **Storage.** Nothing is written to disk and nothing is cached between sessions.
- **Logs.** Logs go to stderr. At `TABLEAU_LOG_LEVEL=DEBUG` they show each HTTP method and
  URL, but never headers, request bodies or secrets.

## Threat model

- **Prompt injection through Tableau content.** Names, descriptions, captions and calculated
  field formulas are written by other people on your site and returned to the model as they
  are. Text in them could try to steer the model. Keep human confirmation on for the three
  write tools (and for write-capable tools of other servers in the same session), and treat
  graph content as data, not instructions.
- **A hostile or misconfigured Tableau host, proxy or network.** Mitigated by required HTTPS,
  verified certificates and refused redirects.
- **Path or query injection through ids.** Node and context ids are percent-encoded as one path
  segment (a `/` or `?` in an id cannot reach another endpoint), and `graph_id` is checked
  against Tableau's documented pattern before it is sent.
- **Supply chain.** One runtime dependency, the MCP SDK.
  - `uv.lock` pins every package by hash.
  - GitHub Actions are pinned by commit SHA.
  - Dependabot updates wait 7 days before adopting a new release, except for security fixes.
  - CodeQL, the OpenSSF Scorecard and a fuzzer for the untrusted-input parsers (error bodies
    from the network, ids from the model) run in CI.
- **Out of scope.** Vulnerabilities in Tableau itself, in your MCP client, or in your model
  provider.

## Verifying a release

Releases are built once in GitHub Actions from the tagged commit.

- **PyPI** uploads use Trusted Publishing. PyPI shows their PEP 740 provenance on each file's
  page.
- **GitHub releases** carry the same wheel and sdist, a CycloneDX SBOM
  (`*.cdx.json`) and a Sigstore signature (`*.sigstore.json`) for each file. To verify a
  download, replace both version numbers with the release you downloaded:

```bash
uvx sigstore verify github --cert-identity https://github.com/biztory/biztory-tableau-knowledge-mcp/.github/workflows/release.yml@refs/tags/v0.1.0 biztory_tableau_knowledge_mcp-0.1.0-py3-none-any.whl
```

## Recommendations

- Use a dedicated PAT with the least privilege required, and rotate it. Reads work for any
  user; writes need the Creator role. A Viewer or Explorer PAT makes the server read-only in
  practice. PATs expire after
  15 days of non-use, or after 1 year.
- Pin a version in production, for example `uvx biztory-tableau-knowledge-mcp==0.1.0`, and review the
  tool list before granting an agent access.
