"""Environment-driven configuration for a Tableau Cloud site with Tableau Knowledge."""

from __future__ import annotations

import os
import re
import urllib.parse
from collections.abc import Mapping
from dataclasses import dataclass

_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}

# The documented pattern for the `graph_id` query parameter.
GRAPH_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


def normalize_server(url: str) -> str:
    """Return a clean ``https://host`` base with no trailing slash or path."""
    url = (url or "").strip().rstrip("/")
    if not url:
        return url
    if url.startswith(("http://", "https://")):
        # keep scheme + host[:port] only
        scheme, rest = url.split("://", 1)
        host = rest.split("/", 1)[0]
        return f"{scheme}://{host}"
    return "https://" + url.split("/", 1)[0]


@dataclass
class Settings:
    """Connection settings, read from environment variables.

    Required: ``TABLEAU_SERVER`` and either a PAT (``TABLEAU_PAT_NAME`` +
    ``TABLEAU_PAT_SECRET``) or a pre-obtained ``TABLEAU_AUTH_TOKEN``.
    """

    server: str
    site_content_url: str = ""
    pat_name: str | None = None
    pat_secret: str | None = None
    auth_token: str | None = None  # advanced: a pre-obtained X-Tableau-Auth token
    api_version: str | None = None  # REST API version for sign-in; else auto-detected
    graph_id: str | None = None  # default graph; else the site's primary graph
    timeout: float = 60.0

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        source: Mapping[str, str] = env if env is not None else os.environ
        server = source.get("TABLEAU_SERVER", "").strip()
        if not server:
            raise RuntimeError(
                "TABLEAU_SERVER is required, e.g. https://10ax.online.tableau.com "
                "(Tableau Knowledge is a Tableau Cloud feature)."
            )
        server = normalize_server(server)
        allow_http = source.get("TABLEAU_ALLOW_HTTP", "").strip().lower() in ("1", "true", "yes")
        parts = urllib.parse.urlsplit(server)
        if parts.scheme == "http" and parts.hostname not in _LOOPBACK_HOSTS and not allow_http:
            raise RuntimeError(
                f"Refusing plain HTTP for {server}: your Tableau credentials would travel unencrypted. "
                "Use https://, or set TABLEAU_ALLOW_HTTP=1 if this is a trusted internal network."
            )
        try:
            timeout = float(source.get("TABLEAU_TIMEOUT", "60"))
        except ValueError:
            timeout = 60.0
        s = cls(
            server=server,
            site_content_url=source.get("TABLEAU_SITE_CONTENT_URL", "").strip(),
            pat_name=(source.get("TABLEAU_PAT_NAME") or None),
            pat_secret=(source.get("TABLEAU_PAT_SECRET") or None),
            auth_token=(source.get("TABLEAU_AUTH_TOKEN") or None),
            api_version=(source.get("TABLEAU_API_VERSION") or None),
            graph_id=(source.get("TABLEAU_KNOWLEDGE_GRAPH_ID", "").strip() or None),
            timeout=timeout,
        )
        s.validate()
        return s

    def validate(self) -> None:
        if self.graph_id and not GRAPH_ID_PATTERN.match(self.graph_id):
            raise RuntimeError("TABLEAU_KNOWLEDGE_GRAPH_ID must be 1-128 letters, digits, '.', '_' or '-'.")
        if self.auth_token:
            return
        if not (self.pat_name and self.pat_secret):
            raise RuntimeError(
                "No credentials. Set TABLEAU_PAT_NAME + TABLEAU_PAT_SECRET "
                "(a Personal Access Token; the secret is the whole string, do not split it), "
                "or TABLEAU_AUTH_TOKEN (a pre-obtained X-Tableau-Auth session token)."
            )
