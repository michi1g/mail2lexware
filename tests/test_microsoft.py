import base64
import hashlib
from unittest.mock import MagicMock
from urllib.parse import parse_qs, urlparse

import pytest
import requests

from mail2lexware import microsoft as ms


def _resp(status=200, body=None):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = body if body is not None else {}
    return r


@pytest.fixture
def post(monkeypatch):
    m = MagicMock()
    monkeypatch.setattr(ms.requests, "post", m)
    return m


def _sent(post):
    args, kwargs = post.call_args
    return args[0], kwargs["data"], kwargs


def test_new_login_url():
    url, pending = ms.new_login("dummy-client-id", "organizations", "a@example.com")
    u = urlparse(url)
    assert f"{u.scheme}://{u.netloc}{u.path}" == "https://login.microsoftonline.com/organizations/oauth2/v2.0/authorize"
    q = {k: v[0] for k, v in parse_qs(u.query).items()}
    assert q["client_id"] == "dummy-client-id"
    assert q["scope"] == ms.SCOPE
    assert q["redirect_uri"] == "http://localhost"
    assert q["response_type"] == "code"
    assert q["response_mode"] == "query"
    assert q["prompt"] == "select_account"
    assert q["login_hint"] == "a@example.com"
    assert q["code_challenge_method"] == "S256"
    assert q["state"] == pending["state"]
    v = pending["code_verifier"]
    assert 43 <= len(v) <= 128
    expected = base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).rstrip(b"=").decode()
    assert q["code_challenge"] == expected
    assert pending["client_id"] == "dummy-client-id"
    assert pending["tenant"] == "organizations"
    assert pending["email"] == "a@example.com"
    assert pending["redirect_uri"] == "http://localhost"


@pytest.mark.parametrize("tenant", [None, "", "  "])
def test_new_login_tenant_default_common(tenant):
    url, pending = ms.new_login("dummy-client-id", tenant, "a@example.com")
    assert "/common/oauth2/v2.0/authorize" in url
    assert pending["tenant"] == "common"


def test_new_login_random():
    _, p1 = ms.new_login("dummy-client-id", "common", "a@example.com")
    _, p2 = ms.new_login("dummy-client-id", "common", "a@example.com")
    assert p1["state"] != p2["state"] and p1["code_verifier"] != p2["code_verifier"]


@pytest.mark.parametrize("client_id,tenant", [("", "common"), (None, "common"), ("   ", "common"),
                                              ("dummy-client-id", "a/b"), ("dummy-client-id", "x?y=1"),
                                              ("dummy-client-id", "evil.com/../x")])
def test_new_login_invalid(client_id, tenant):
    with pytest.raises(ms.MicrosoftAuthError):
        ms.new_login(client_id, tenant, "a@example.com")


def _token_body(**kw):
    b = {"access_token": "dummy-token", "refresh_token": "dummy-refresh", "expires_in": 3600}
    b.update(kw)
    return b


def test_finish_login_full_url(post):
    _, pending = ms.new_login("dummy-client-id", "contoso.com", "a@example.com")
    post.return_value = _resp(200, _token_body())
    url = f"http://localhost/?code=dummy-code&state={pending['state']}&session_state=x"
    t = ms.finish_login(pending, url, now=1000)
    assert t == {"client_id": "dummy-client-id", "tenant": "contoso.com", "access_token": "dummy-token",
                 "refresh_token": "dummy-refresh", "expires_at": 4600}
    target, data, kwargs = _sent(post)
    assert target == "https://login.microsoftonline.com/contoso.com/oauth2/v2.0/token"
    assert data["grant_type"] == "authorization_code"
    assert data["client_id"] == "dummy-client-id"
    assert data["code"] == "dummy-code"
    assert data["redirect_uri"] == "http://localhost"
    assert data["code_verifier"] == pending["code_verifier"]
    assert data["scope"] == ms.SCOPE
    assert kwargs["timeout"] == 20


def test_finish_login_bare_code(post):
    _, pending = ms.new_login("dummy-client-id", "", "a@example.com")
    post.return_value = _resp(200, _token_body())
    t = ms.finish_login(pending, "  dummy-code \n", now=0)
    assert t["tenant"] == "common"
    target, data, _ = _sent(post)
    assert target.startswith("https://login.microsoftonline.com/common/")
    assert data["code"] == "dummy-code"


def test_finish_login_state_mismatch(post):
    _, pending = ms.new_login("dummy-client-id", "common", "a@example.com")
    with pytest.raises(ms.MicrosoftAuthError):
        ms.finish_login(pending, "http://localhost/?code=dummy-code&state=falsch")
    with pytest.raises(ms.MicrosoftAuthError):
        ms.finish_login(pending, "http://localhost/?code=dummy-code")
    post.assert_not_called()


def test_finish_login_error_in_url(post):
    _, pending = ms.new_login("dummy-client-id", "common", "a@example.com")
    url = "http://localhost/?error=access_denied&error_description=Der+Nutzer+hat+abgelehnt"
    with pytest.raises(ms.MicrosoftAuthError) as e:
        ms.finish_login(pending, url)
    assert "access_denied" in str(e.value) and "abgelehnt" in str(e.value)
    post.assert_not_called()


def test_finish_login_without_refresh_token(post):
    _, pending = ms.new_login("dummy-client-id", "common", "a@example.com")
    post.return_value = _resp(200, {"access_token": "dummy-token", "expires_in": 3600})
    with pytest.raises(ms.MicrosoftAuthError) as e:
        ms.finish_login(pending, "dummy-code")
    assert "offline_access" in str(e.value)
    assert "dummy-token" not in str(e.value)


def test_finish_login_error_response(post):
    _, pending = ms.new_login("dummy-client-id", "common", "a@example.com")
    post.return_value = _resp(400, {"error": "invalid_client", "error_description": "AADSTS700016: App nicht gefunden\r\nTrace ID: x"})
    with pytest.raises(ms.MicrosoftAuthError) as e:
        ms.finish_login(pending, "dummy-code")
    assert "invalid_client" in str(e.value) and "AADSTS700016" in str(e.value)
    assert "Trace" not in str(e.value)


def test_finish_login_http_error_without_json(post):
    _, pending = ms.new_login("dummy-client-id", "common", "a@example.com")
    r = _resp(502)
    r.json.side_effect = ValueError
    post.return_value = r
    with pytest.raises(ms.MicrosoftAuthError):
        ms.finish_login(pending, "dummy-code")


def test_finish_login_network_error(post):
    _, pending = ms.new_login("dummy-client-id", "common", "a@example.com")
    post.side_effect = requests.ConnectionError("boom dummy-code")
    with pytest.raises(ms.MicrosoftAuthError) as e:
        ms.finish_login(pending, "dummy-code")
    assert "dummy-code" not in str(e.value)


def _tokens(expires_at):
    return {"client_id": "dummy-client-id", "tenant": "common", "access_token": "alt-token",
            "refresh_token": "alt-refresh", "expires_at": expires_at}


def test_ensure_no_refresh(post):
    t = _tokens(1000 + 500)
    orig = dict(t)
    token, out, changed = ms.ensure_access_token(t, now=1000)
    assert (token, changed) == ("alt-token", False)
    assert out == orig and t == orig
    post.assert_not_called()


def test_ensure_refresh_with_new_refresh_token(post):
    t = _tokens(1000 + 60)
    orig = dict(t)
    post.return_value = _resp(200, _token_body(access_token="neu-token", refresh_token="neu-refresh", expires_in=1800))
    token, out, changed = ms.ensure_access_token(t, now=1000)
    assert (token, changed) == ("neu-token", True)
    assert out["refresh_token"] == "neu-refresh" and out["expires_at"] == 2800
    assert out["client_id"] == "dummy-client-id" and out["tenant"] == "common"
    assert t == orig  # Eingabe unverändert
    target, data, kwargs = _sent(post)
    assert target == "https://login.microsoftonline.com/common/oauth2/v2.0/token"
    assert data == {"grant_type": "refresh_token", "client_id": "dummy-client-id",
                    "refresh_token": "alt-refresh", "scope": ms.SCOPE}
    assert kwargs["timeout"] == 20


def test_ensure_refresh_keeps_old_refresh_token(post):
    post.return_value = _resp(200, {"access_token": "neu-token", "expires_in": 3600})
    _, out, changed = ms.ensure_access_token(_tokens(0), now=1000)
    assert changed and out["refresh_token"] == "alt-refresh" and out["expires_at"] == 4600


def test_ensure_invalid_grant(post):
    post.return_value = _resp(400, {"error": "invalid_grant", "error_description": "AADSTS70000: abgelaufen"})
    with pytest.raises(ms.MicrosoftAuthError) as e:
        ms.ensure_access_token(_tokens(0), now=1000)
    assert "erneut" in str(e.value)


def test_xoauth2_string():
    assert ms.xoauth2_string("a@example.com", "dummy-token") == "user=a@example.com\x01auth=Bearer dummy-token\x01\x01"
