"""Transport/auth tests with a mocked urllib layer (no network)."""

import io
import json
import time
import urllib.error

import pytest
from conftest import Resp, http_error, kerr

from biztory_tableau_knowledge_mcp import client as client_mod
from biztory_tableau_knowledge_mcp.client import TableauClient, TableauError, describe_http_error
from biztory_tableau_knowledge_mcp.config import Settings


def _patch(monkeypatch, handler):
    monkeypatch.setattr(client_mod._OPENER, "open", lambda req, timeout=None: handler(req))


def _client(**env):
    base = {"TABLEAU_SERVER": "https://h.com", "TABLEAU_PAT_NAME": "n", "TABLEAU_PAT_SECRET": "sec"}
    base.update(env)
    return TableauClient(Settings.from_env(base))


def test_api_version_uses_versioned_serverinfo(monkeypatch):
    seen = []

    def h(req):
        seen.append(req.full_url)
        return Resp({"serverInfo": {"restApiVersion": "3.30"}})

    _patch(monkeypatch, h)
    assert _client().api_version() == "3.30"
    assert seen == ["https://h.com/api/3.4/serverinfo"]  # bare /api/serverinfo is 404 on Cloud


def test_api_version_fallback_on_error(monkeypatch):
    def h(req):
        raise http_error(req.full_url, 404, b"")

    _patch(monkeypatch, h)
    assert _client().api_version() == "3.4"


def test_signin_returns_and_caches_token(monkeypatch):
    calls = {"signin": 0}

    def h(req):
        if "serverinfo" in req.full_url:
            return Resp({"serverInfo": {"restApiVersion": "3.30"}})
        assert req.full_url == "https://h.com/api/3.30/auth/signin"
        calls["signin"] += 1
        body = json.loads(req.data)
        assert body["credentials"]["personalAccessTokenName"] == "n"
        assert body["credentials"]["personalAccessTokenSecret"] == "sec"  # never split
        assert body["credentials"]["site"] == {"contentUrl": "mysite"}
        return Resp({"credentials": {"token": "TOK", "site": {"id": "s1"}}})

    _patch(monkeypatch, h)
    c = _client(TABLEAU_SITE_CONTENT_URL="mysite")
    assert c._get_token() == "TOK"
    assert c._get_token() == "TOK"  # cached
    assert calls["signin"] == 1


def test_signin_401_raises_with_hint_and_no_secret(monkeypatch):
    def h(req):
        if "serverinfo" in req.full_url:
            return Resp({"serverInfo": {"restApiVersion": "3.30"}})
        raise http_error(
            req.full_url, 401, b'{"error":{"detail":"The personal access token you provided is invalid."}}'
        )

    _patch(monkeypatch, h)
    with pytest.raises(TableauError) as e:
        _client(TABLEAU_PAT_SECRET="s3cr3t-VALUE")._get_token()
    assert "invalid" in str(e.value) and "15 days" in str(e.value)
    assert "s3cr3t-VALUE" not in str(e.value)


def test_knowledge_builds_url_query_and_body(monkeypatch):
    seen = {}

    def h(req):
        seen.update(
            url=req.full_url,
            method=req.get_method(),
            body=json.loads(req.data),
            auth=req.get_header("X-tableau-auth"),
        )
        return Resp({"ok": 1})

    _patch(monkeypatch, h)
    c = _client(TABLEAU_AUTH_TOKEN="PRE")
    out = c.knowledge(
        "POST", "/nodes/search", query={"graph_id": "g1", "skip": None, "flag": False}, body={"query": "x"}
    )
    assert out == {"ok": 1}
    assert seen == {
        "url": "https://h.com/api/v1/knowledge/nodes/search?graph_id=g1&flag=false",
        "method": "POST",
        "body": {"query": "x"},
        "auth": "PRE",
    }


def test_knowledge_204_returns_none(monkeypatch):
    _patch(monkeypatch, lambda req: Resp(None, 204))
    assert _client(TABLEAU_AUTH_TOKEN="PRE").knowledge("DELETE", "/semantic-contexts/x") is None


def test_knowledge_resigns_in_once_on_401(monkeypatch):
    calls = {"signin": 0, "api": 0}

    def h(req):
        if "auth/signin" in req.full_url:
            calls["signin"] += 1
            return Resp({"credentials": {"token": f"T{calls['signin']}"}})
        calls["api"] += 1
        if req.get_header("X-tableau-auth") == "T1":
            raise http_error(req.full_url, 401, kerr("auth_failed"))
        return Resp({"graphs": []})

    _patch(monkeypatch, h)
    c = _client(TABLEAU_API_VERSION="3.30")
    assert c.knowledge("GET", "/graphs") == {"graphs": []}
    assert calls == {"signin": 2, "api": 2}


def test_knowledge_persistent_401_explains_scope(monkeypatch):
    def h(req):
        if "auth/signin" in req.full_url:
            return Resp({"credentials": {"token": "T"}})
        raise http_error(req.full_url, 401, kerr("auth_failed", "Missing scope"))

    _patch(monkeypatch, h)
    with pytest.raises(TableauError, match="tableau:knowledge") as e:
        _client(TABLEAU_API_VERSION="3.30").knowledge("GET", "/graphs")
    assert e.value.code == "auth_failed"


def test_preset_token_does_not_retry_on_401(monkeypatch):
    calls = []

    def h(req):
        calls.append(req.full_url)
        raise http_error(req.full_url, 401, kerr("auth_failed"))

    _patch(monkeypatch, h)
    with pytest.raises(TableauError):
        _client(TABLEAU_AUTH_TOKEN="PRE").knowledge("GET", "/graphs")
    assert len(calls) == 1


def test_non_json_response_is_clean_error(monkeypatch):
    class Html(Resp):
        def read(self):
            return b"<html>login</html>"

    _patch(monkeypatch, lambda req: Html({}))
    with pytest.raises(TableauError, match="non-JSON"):
        _client(TABLEAU_AUTH_TOKEN="PRE").knowledge("GET", "/graphs")


def test_unreachable_is_clean_error(monkeypatch):
    def h(req):
        raise urllib.error.URLError("connection refused")

    _patch(monkeypatch, h)
    with pytest.raises(TableauError, match="unreachable"):
        _client(TABLEAU_AUTH_TOKEN="PRE").knowledge("GET", "/graphs")


@pytest.mark.parametrize(
    ("code", "body", "expect"),
    [
        (404, kerr("graph_not_found"), "server_info"),
        (409, kerr("no_active_graph"), "primary graph"),
        (403, kerr("tableau_forbidden"), "Creator"),
        (503, kerr("tableau_unavailable"), "Retry"),
        (403, kerr("node_semantic_context_disabled", "Node-scoped context disabled"), "is_global=true"),
        (503, kerr("internal", "No embedder configured"), "Semantic search is unavailable"),
        (500, kerr("weird_new_code", "boom"), "weird_new_code: boom"),
    ],
)
def test_error_codes_become_next_steps(code, body, expect):
    msg, ecode = describe_http_error(code, "error", json.dumps(body).encode())
    assert expect in msg
    assert "trace_id t-1" in msg
    assert ecode == body["code"]


def test_422_lists_invalid_fields():
    body = {"detail": [{"loc": ["body", "threshold"], "msg": "must be <= 1", "type": "x"}]}
    msg, ecode = describe_http_error(422, "Unprocessable", json.dumps(body).encode())
    assert msg == "HTTP 422 invalid request: threshold: must be <= 1"
    assert ecode == "unprocessable_content"


def test_non_json_error_body_is_kept_short():
    msg, ecode = describe_http_error(502, "Bad Gateway", b"<html>" + b"x" * 1000)
    assert msg.startswith("HTTP 502: <html>") and len(msg) < 400 and ecode == ""


def test_server_info_reports_graphs(monkeypatch):
    def h(req):
        if "serverinfo" in req.full_url:
            return Resp({"serverInfo": {"productVersion": {"value": "2026.3.2"}, "restApiVersion": "3.30"}})
        return Resp({"graphs": []})

    _patch(monkeypatch, h)
    info = _client(TABLEAU_AUTH_TOKEN="PRE", TABLEAU_SITE_CONTENT_URL="biz").server_info()
    assert info["product_version"] == "2026.3.2"
    assert info["site_content_url"] == "biz"
    assert info["knowledge_available"] is True
    assert "no graphs" in info["note"]


def test_server_info_tolerates_serverinfo_failure(monkeypatch):
    def h(req):
        if "serverinfo" in req.full_url:
            raise urllib.error.URLError("down")
        return Resp({"graphs": [{"id": "g"}]})

    _patch(monkeypatch, h)
    info = _client(TABLEAU_AUTH_TOKEN="PRE").server_info()
    assert "product_version" not in info and info["graphs"] == [{"id": "g"}]


def test_signout_clears_token(monkeypatch):
    seen = []
    _patch(monkeypatch, lambda req: seen.append(req.full_url) or Resp(None, 204))
    c = _client(TABLEAU_API_VERSION="3.30")
    c._token = "T"
    c.signout()
    assert c._token is None and seen == ["https://h.com/api/3.30/auth/signout"]


def _signin_counter(monkeypatch, ttl):
    calls = {"signin": 0}

    def h(req):
        calls["signin"] += 1
        return Resp({"credentials": {"token": f"T{calls['signin']}", "estimatedTimeToExpiration": ttl}})

    _patch(monkeypatch, h)
    return _client(TABLEAU_API_VERSION="3.30"), calls


def test_token_reused_until_reported_expiry(monkeypatch):
    c, calls = _signin_counter(monkeypatch, "239:59:30")
    assert c._get_token() == c._get_token() == "T1"
    assert calls["signin"] == 1


def test_token_refreshed_once_reported_expiry_passes(monkeypatch):
    c, calls = _signin_counter(monkeypatch, "0:00:30")  # inside the 60 s safety margin
    assert c._get_token() == "T1"
    assert c._get_token() == "T2"
    assert calls["signin"] == 2


def test_token_deadline_is_monotonic(monkeypatch):
    c, _ = _signin_counter(monkeypatch, "1:00:00")
    c._get_token()
    assert c._token_deadline is not None
    assert 3500 < c._token_deadline - time.monotonic() <= 3540


def test_unexpected_errors_are_not_swallowed(monkeypatch):
    def boom(req):
        raise RuntimeError("bug")

    _patch(monkeypatch, boom)
    with pytest.raises(RuntimeError):
        _client(TABLEAU_AUTH_TOKEN="PRE").knowledge("GET", "/graphs")


def test_redirect_is_refused_and_credentials_never_leave():
    """A 302 to another origin must not be followed: urllib would resend X-Tableau-Auth there."""
    import http.server
    import threading

    leaked: dict[str, str] = {}

    class Target(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            leaked.update(self.headers.items())
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *a):
            pass

    target = http.server.HTTPServer(("127.0.0.1", 0), Target)

    class Redirector(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{target.server_port}/steal")
            self.end_headers()

        def log_message(self, *a):
            pass

    redirector = http.server.HTTPServer(("127.0.0.1", 0), Redirector)
    for srv in (target, redirector):
        threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        c = _client(TABLEAU_SERVER=f"http://localhost:{redirector.server_port}", TABLEAU_AUTH_TOKEN="SECRET")
        with pytest.raises(TableauError, match="redirect") as exc:
            c.knowledge("GET", "/graphs")
        assert f"127.0.0.1:{target.server_port}" in str(exc.value)  # names where it tried to send you
        assert "SECRET" not in str(exc.value)
        assert leaked == {}
    finally:
        for srv in (target, redirector):
            srv.shutdown()


def _rate_limited(monkeypatch, failures, retry_after="2"):
    """Answer `failures` requests with 429, then succeed. Returns (client, recorded sleeps)."""
    calls, sleeps = {"n": 0}, []
    monkeypatch.setattr(client_mod.time, "sleep", sleeps.append)

    def h(req):
        calls["n"] += 1
        if calls["n"] <= failures:
            headers = {"Retry-After": retry_after} if retry_after is not None else {}
            raise urllib.error.HTTPError(
                req.full_url, 429, "Too Many", headers, io.BytesIO(b'{"message":"Rate limit exceeded."}')
            )
        return Resp({"graphs": []})

    _patch(monkeypatch, h)
    return _client(TABLEAU_AUTH_TOKEN="PRE"), sleeps


def test_429_is_retried_honouring_retry_after(monkeypatch):
    c, sleeps = _rate_limited(monkeypatch, failures=2)
    assert c.knowledge("GET", "/graphs") == {"graphs": []}
    assert sleeps == [2.0, 2.0]


def test_429_without_retry_after_backs_off_exponentially(monkeypatch):
    c, sleeps = _rate_limited(monkeypatch, failures=3, retry_after=None)
    assert c.knowledge("GET", "/graphs") == {"graphs": []}
    assert sleeps == [1.0, 2.0, 4.0]


def test_429_wait_is_capped_and_http_dates_fall_back(monkeypatch):
    c, sleeps = _rate_limited(monkeypatch, failures=1, retry_after="3600")
    c.knowledge("GET", "/graphs")
    c2, sleeps2 = _rate_limited(monkeypatch, failures=1, retry_after="Wed, 21 Oct 2026 07:28:00 GMT")
    c2.knowledge("GET", "/graphs")
    assert sleeps == [30.0] and sleeps2 == [1.0]


def test_persistent_429_gives_up_with_a_hint(monkeypatch):
    c, sleeps = _rate_limited(monkeypatch, failures=99)
    with pytest.raises(TableauError, match="already retried") as e:
        c.knowledge("GET", "/graphs")
    assert len(sleeps) == 3 and "HTTP 429" in str(e.value)


def test_malformed_422_entries_never_crash():
    """The error body is untrusted network input (found while writing the fuzz harness)."""
    body = {
        "detail": [
            {"loc": 7, "msg": "x"},
            {"loc": None},
            "junk",
            {"loc": ["body", "limit"], "msg": "too big"},
        ]
    }
    msg, _ = describe_http_error(400, "Bad", json.dumps(body).encode())
    assert msg.startswith("HTTP 400 invalid request: ")  # the real status, not a hard-coded 422
    assert "limit: too big" in msg


def test_huge_error_message_is_truncated():
    msg, ecode = describe_http_error(
        500, "err", json.dumps({"code": "c" * 1000, "message": "m" * 10_000}).encode()
    )
    assert len(msg) < 700 and len(ecode) == 80
