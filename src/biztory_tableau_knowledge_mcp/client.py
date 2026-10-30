"""Transport + auth for the Tableau Knowledge Graph API (``/api/v1/knowledge``).

Stdlib only (urllib). Auth: a PAT sign-in (recommended) or a pre-obtained X-Tableau-Auth
token. The Knowledge API is always ``v1``; the classic REST version is only needed for
sign-in and is auto-detected. Tableau's error envelope (``code``/``message``/``trace_id``)
is turned into a short message plus a concrete next step the model can act on.
"""

from __future__ import annotations

import contextlib
import http.client
import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from email.message import Message
from typing import IO, Any

from .config import Settings

log = logging.getLogger("biztory-tableau-knowledge-mcp")

KNOWLEDGE_BASE = "/api/v1/knowledge"

# What a best-effort HTTP call can actually raise: network/timeout (OSError, incl. URLError),
# a bad response (HTTPException), non-JSON (ValueError), or an unexpected JSON shape.
_SOFT_ERRORS = (OSError, http.client.HTTPException, ValueError, AttributeError, KeyError, TypeError)

# Tableau Knowledge error codes -> what to do about them. Shown after the server's own message.
_HINTS = {
    "auth_failed": (
        "The session is missing, expired, or lacks the tableau:knowledge:read/write scope. "
        "Check the PAT and TABLEAU_SITE_CONTENT_URL."
    ),
    "site_not_entitled": (
        "This site doesn't have Tableau Knowledge (Tableau Cloud, Tableau+ license). "
        "A Tableau Cloud Manager admin has to enable it."
    ),
    "tableau_forbidden": (
        "Your Tableau role lacks the capability for this call. Writing semantic contexts needs the Creator role."
    ),
    "graph_not_found": "That graph_id isn't on this site. Call server_info to list the site's graphs.",
    "no_active_graph": (
        "The site has no primary graph. Pass a graph_id from server_info, or build a graph first."
    ),
    "not_found": "No node with that id is visible to you. Use search_nodes to find the right id.",
    "conflict": (
        "Tableau-managed (SEMANTIC_CONTEXT_EXTERNAL) contexts are read-only; only contexts created "
        "through this API can be changed or deleted."
    ),
    "tableau_unavailable": "Tableau's auth/site service is briefly unavailable. Retry the call.",
    "node_semantic_context_disabled": (
        "This site accepts only graph-wide context through the API; node-attached context is managed by "
        "Tableau through ingestion. Use is_global=true and name the field or data source in each statement "
        "(import_from_ossie does this for you)."
    ),
}
_RATE_LIMIT_HINT = (
    "Tableau rate-limits the Knowledge API and this server already retried with back-off. "
    "Wait a minute, or answer with fewer calls (e.g. one lineage call instead of many get_node calls)."
)
# 429 handling: retry this many times, waiting Retry-After (or 1, 2, 4 s), never longer than the cap.
_MAX_RETRIES = 3
_MAX_WAIT = 30.0
_NO_EMBEDDER_HINT = (
    "Semantic search is unavailable on this graph (no embedder configured). "
    "Use exact node ids with get_node, lineage or impact_analysis, or browse with list_sources."
)


class _RefuseRedirects(urllib.request.HTTPRedirectHandler):
    """Never follow redirects: urllib would resend X-Tableau-Auth to the new host,
    even a different one. Credentials go to TABLEAU_SERVER and nowhere else."""

    def redirect_request(
        self, req: urllib.request.Request, fp: IO[bytes], code: int, msg: str, headers: Message, newurl: str
    ) -> urllib.request.Request | None:
        raise urllib.error.HTTPError(
            req.full_url,
            code,
            f"redirect to {newurl} refused (credentials are only sent to TABLEAU_SERVER; "
            "if that is your server's real address, set TABLEAU_SERVER to it)",
            headers,
            fp,
        )


_OPENER = urllib.request.build_opener(_RefuseRedirects)


def _expiry_deadline(ttl: Any) -> float | None:
    """Sign-in's ``estimatedTimeToExpiration`` ("HHH:MM:SS") -> a monotonic deadline, 60 s early.

    None when Tableau doesn't report it; the 401 re-sign-in still covers that.
    """
    try:
        h, m, s = (int(x) for x in str(ttl).split(":"))
    except ValueError:
        return None
    return time.monotonic() + h * 3600 + m * 60 + s - 60


def _retry_wait(e: urllib.error.HTTPError, attempt: int) -> float:
    """Seconds to wait before retrying a 429: the server's Retry-After, else 1, 2, 4 s; capped."""
    try:
        wait = float(e.headers.get("Retry-After") or "")
    except (TypeError, ValueError):  # absent, or an HTTP date
        wait = 2.0**attempt
    return max(0.0, min(wait, _MAX_WAIT))


def segment(value: str) -> str:
    """Percent-encode one path segment, so an id can never add path parts or a query string."""
    return urllib.parse.quote(value, safe="")


# The error body comes from the network: keep what reaches the model short.
_MAX_MESSAGE = 500


def _invalid_field(d: dict[str, Any]) -> str:
    """One 422 entry -> "field.path: message". ``loc`` is ["body", "field", ...] when well-formed."""
    loc = d.get("loc")
    path = ".".join(str(p) for p in loc[1:]) if isinstance(loc, list) else ""
    return f"{path}: {d.get('msg', '')}"


def describe_http_error(code: int, reason: str, body: bytes) -> tuple[str, str]:
    """Build a model-readable message from a Knowledge API error response -> (message, error code)."""
    try:
        err = json.loads(body.decode("utf-8", "replace")) if body else {}
    except ValueError:
        err = {}
    if not isinstance(err, dict):
        err = {}
    if isinstance(err.get("detail"), list):  # 422: one entry per invalid field
        fields = "; ".join(_invalid_field(d) for d in err["detail"] if isinstance(d, dict))
        return f"HTTP {code} invalid request: {fields[:_MAX_MESSAGE]}", "unprocessable_content"
    ecode = str(err.get("code") or "")[:80]
    msg = str(err.get("message") or err.get("detail") or "").strip()[:_MAX_MESSAGE]
    if not msg:
        msg = body.decode("utf-8", "replace")[:300].strip() if body else reason
    out = f"HTTP {code}" + (f" {ecode}" if ecode else "") + f": {msg}"
    if code == 429:
        hint: str | None = _RATE_LIMIT_HINT
    elif code == 503 and ecode in ("", "internal"):
        hint = _NO_EMBEDDER_HINT
    else:
        hint = _HINTS.get(ecode)
    if hint:
        out += f" {hint}"
    if err.get("trace_id"):
        out += f" (trace_id {err['trace_id']})"
    return out, ecode


class TableauError(RuntimeError):
    """A clean, model-readable error (no stack traces, no secrets).

    ``code`` is the Knowledge API's machine-readable error code when there is one."""

    def __init__(self, message: str, code: str = ""):
        super().__init__(message)
        self.code = code


class TableauClient:
    def __init__(self, settings: Settings):
        self.s = settings
        self._lock = threading.Lock()
        self._token: str | None = settings.auth_token
        self._token_deadline: float | None = None  # only for PAT sessions that report a lifetime
        self._api_version: str | None = settings.api_version
        if settings.server.startswith("http://"):
            log.warning("TABLEAU_SERVER uses plain HTTP: credentials and results travel unencrypted.")

    # ---------- low-level HTTP ----------
    def _request(
        self,
        url: str,
        *,
        method: str = "GET",
        data: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        h = {"Accept": "application/json"}
        if data is not None:
            h["Content-Type"] = "application/json"
        h.update(headers or {})
        # Method + URL only: never headers (auth token) or body (PAT secret).
        log.debug("%s %s", method, url)
        req = urllib.request.Request(url, data=data, headers=h, method=method)
        with _OPENER.open(req, timeout=self.s.timeout) as r:
            body = r.read()
        return json.loads(body) if body else None

    @staticmethod
    def _read_error(e: urllib.error.HTTPError) -> bytes:
        try:
            return e.read()[:4000]
        except (OSError, http.client.HTTPException):
            return b""

    # ---------- REST API version (sign-in only) ----------
    def api_version(self) -> str:
        if self._api_version:
            return self._api_version
        try:
            # Versioned path: the bare /api/serverinfo is 404 on Tableau Cloud.
            d = self._request(f"{self.s.server}/api/3.4/serverinfo")
            self._api_version = d["serverInfo"]["restApiVersion"] or "3.4"
        except _SOFT_ERRORS as exc:  # any network/parse failure -> safe floor
            log.warning("serverinfo failed (%s); defaulting REST API version to 3.4", exc)
            self._api_version = "3.4"
        return self._api_version

    # ---------- auth ----------
    def _token_expired(self) -> bool:
        return self._token_deadline is not None and time.monotonic() >= self._token_deadline

    def _get_token(self, *, force: bool = False) -> str:
        with self._lock:
            if self._token and not force and not self._token_expired():
                return self._token
            s = self.s
            if not (s.pat_name and s.pat_secret):
                if self._token:
                    return self._token
                raise TableauError("No credentials configured for sign-in.")
            body = json.dumps(
                {
                    "credentials": {
                        "personalAccessTokenName": s.pat_name,
                        "personalAccessTokenSecret": s.pat_secret,
                        "site": {"contentUrl": s.site_content_url},
                    }
                }
            ).encode()
            url = f"{s.server}/api/{self.api_version()}/auth/signin"
            log.info("signing in to %s (PAT)", s.server)
            try:
                cred = self._request(url, method="POST", data=body)["credentials"]
            except urllib.error.HTTPError as e:
                hint = ""
                if e.code == 401:
                    hint = (
                        " Check the PAT name/secret (PATs expire after 15 days unused) "
                        "and TABLEAU_SITE_CONTENT_URL."
                    )
                detail = self._read_error(e).decode("utf-8", "replace")[:300]
                raise TableauError(f"Sign-in failed: HTTP {e.code}. {detail}{hint}") from None
            except (urllib.error.URLError, KeyError, TypeError, ValueError) as e:
                raise TableauError(f"Sign-in failed: {e}") from None
            self._token = str(cred["token"])
            self._token_deadline = _expiry_deadline(cred.get("estimatedTimeToExpiration"))
            return self._token

    def signout(self) -> None:
        if self._token and not self.s.auth_token:
            with contextlib.suppress(Exception):  # best effort on shutdown
                self._request(
                    f"{self.s.server}/api/{self.api_version()}/auth/signout",
                    method="POST",
                    data=b"",
                    headers={"X-Tableau-Auth": self._token},
                )
            self._token = None
            self._token_deadline = None

    # ---------- Knowledge API ----------
    def knowledge(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
    ) -> Any:
        """Call ``/api/v1/knowledge{path}``. Returns parsed JSON, or None for an empty (204) body.

        ``path`` must already have its ids encoded with :func:`segment`. ``None`` query values are
        dropped; booleans are sent as ``true``/``false``.
        """
        params = {
            k: (str(v).lower() if isinstance(v, bool) else str(v))
            for k, v in (query or {}).items()
            if v is not None
        }
        url = f"{self.s.server}{KNOWLEDGE_BASE}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = json.dumps(body).encode() if body is not None else None

        def call() -> Any:
            for attempt in range(_MAX_RETRIES + 1):
                try:
                    return self._request(
                        url, method=method, data=data, headers={"X-Tableau-Auth": self._get_token()}
                    )
                except urllib.error.HTTPError as e:
                    if e.code != 429 or attempt == _MAX_RETRIES:
                        raise
                    wait = _retry_wait(e, attempt)
                    log.warning("rate limited (HTTP 429); retrying in %.1f s", wait)
                    time.sleep(wait)
            raise AssertionError("unreachable")  # pragma: no cover

        try:
            try:
                return call()
            except urllib.error.HTTPError as e:
                # A PAT session can expire mid-run: sign in again once, then give up.
                if e.code == 401 and not self.s.auth_token:
                    self._get_token(force=True)
                    return call()
                raise
        except urllib.error.HTTPError as e:
            raise TableauError(*describe_http_error(e.code, str(e.reason), self._read_error(e))) from None
        except urllib.error.URLError as e:
            raise TableauError(f"Tableau Knowledge API unreachable: {e.reason}") from None
        except ValueError:  # e.g. an HTML login/proxy page instead of JSON
            raise TableauError("Tableau Knowledge API returned a non-JSON response.") from None

    # ---------- context ----------
    def server_info(self) -> dict[str, Any]:
        info: dict[str, Any] = {
            "server": self.s.server,
            "site_content_url": self.s.site_content_url or "(default)",
            "auth": "token" if self.s.auth_token else "pat",
            "default_graph_id": self.s.graph_id or "(site's primary graph)",
        }
        try:
            si = self._request(f"{self.s.server}/api/3.4/serverinfo")["serverInfo"]
            pv = si.get("productVersion")
            info["product_version"] = pv.get("value") if isinstance(pv, dict) else pv
            info["rest_api_version"] = si.get("restApiVersion")
        except _SOFT_ERRORS as exc:  # version info is optional context
            log.debug("serverinfo unavailable: %s", exc)
        try:
            graphs = (self.knowledge("GET", "/graphs") or {}).get("graphs") or []
        except TableauError as e:
            info["knowledge_available"] = False
            info["knowledge_error"] = str(e)
            return info
        info["knowledge_available"] = True
        info["graphs"] = graphs
        if not graphs:
            info["note"] = "Tableau Knowledge is enabled but this site has no graphs yet."
        return info
