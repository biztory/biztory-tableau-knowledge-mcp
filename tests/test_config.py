import pytest

from biztory_tableau_knowledge_mcp.config import Settings, normalize_server


def test_normalize_server():
    assert normalize_server("tableau.company.com") == "https://tableau.company.com"
    assert normalize_server("https://x.online.tableau.com/") == "https://x.online.tableau.com"
    assert (
        normalize_server("https://10ax.online.tableau.com/#/site/biz/home")
        == "https://10ax.online.tableau.com"
    )
    assert normalize_server("http://h:8000/") == "http://h:8000"


def test_from_env_pat_keeps_secret_intact():
    s = Settings.from_env(
        {
            "TABLEAU_SERVER": "https://h.com",
            "TABLEAU_SITE_CONTENT_URL": " biz ",
            "TABLEAU_PAT_NAME": "n",
            "TABLEAU_PAT_SECRET": "abc==:def",  # secrets can contain ':', must not be split
        }
    )
    assert s.server == "https://h.com"
    assert s.site_content_url == "biz"
    assert s.pat_secret == "abc==:def"
    assert s.graph_id is None


def test_missing_server_raises():
    with pytest.raises(RuntimeError, match="TABLEAU_SERVER"):
        Settings.from_env({})


def test_missing_auth_raises():
    with pytest.raises(RuntimeError, match="No credentials"):
        Settings.from_env({"TABLEAU_SERVER": "https://h.com"})


def test_token_auth_is_valid():
    assert Settings.from_env({"TABLEAU_SERVER": "https://h.com", "TABLEAU_AUTH_TOKEN": "t"}).auth_token == "t"


_PAT = {"TABLEAU_PAT_NAME": "n", "TABLEAU_PAT_SECRET": "s"}


def test_default_graph_id():
    s = Settings.from_env({"TABLEAU_SERVER": "https://h.com", "TABLEAU_KNOWLEDGE_GRAPH_ID": " g-1 ", **_PAT})
    assert s.graph_id == "g-1"


def test_invalid_default_graph_id_is_refused():
    with pytest.raises(RuntimeError, match="TABLEAU_KNOWLEDGE_GRAPH_ID"):
        Settings.from_env({"TABLEAU_SERVER": "https://h.com", "TABLEAU_KNOWLEDGE_GRAPH_ID": "a b", **_PAT})


def test_bad_timeout_falls_back():
    assert (
        Settings.from_env({"TABLEAU_SERVER": "https://h.com", "TABLEAU_TIMEOUT": "soon", **_PAT}).timeout
        == 60.0
    )


@pytest.mark.parametrize("server", ["http://tableau.company.com", "http://10.0.0.5:8000"])
def test_plain_http_is_refused(server):
    with pytest.raises(RuntimeError, match="TABLEAU_ALLOW_HTTP"):
        Settings.from_env({"TABLEAU_SERVER": server, **_PAT})


@pytest.mark.parametrize("server", ["http://localhost:8000", "http://127.0.0.1", "http://[::1]:8080"])
def test_plain_http_allowed_on_loopback(server):
    assert Settings.from_env({"TABLEAU_SERVER": server, **_PAT}).server == server


def test_plain_http_allowed_with_explicit_opt_in():
    s = Settings.from_env({"TABLEAU_SERVER": "http://tableau.intra", "TABLEAU_ALLOW_HTTP": "1", **_PAT})
    assert s.server == "http://tableau.intra"
