"""Tests for the /link self-service PAT registration page.

Focus on the branches that don't require a live Google / Plane (security-critical:
state binding, XSS escaping, CSRF, session gating). Network-dependent success paths
(Google token exchange, PAT validation) are exercised manually / left for integration.
"""

import asyncio

import pytest
from cryptography.fernet import Fernet
from starlette.applications import Starlette
from starlette.testclient import TestClient

import plane_mcp.user_pat_store as ups
from plane_mcp import link_app
from plane_mcp.user_pat_store import UserPatStore


@pytest.fixture
def store(tmp_path, monkeypatch):
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("MCP_PAT_ENC_KEY", key)
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAIN", "slogin.io")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://plane.slogin.io")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "test-cid")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "test-secret")
    monkeypatch.setenv("PLANE_INTERNAL_BASE_URL", "http://plane.local")
    s = UserPatStore(db_path=str(tmp_path / "pat.db"), enc_key=key)
    monkeypatch.setattr(ups, "_store", s)  # inject the singleton used by link_app
    return s


@pytest.fixture
def client(store):
    app = Starlette(routes=link_app.get_link_routes())
    return TestClient(app, follow_redirects=False)


def _session(email: str) -> str:
    return link_app._sign("session", {"email": email})


def test_index_anonymous_shows_login(client):
    r = client.get("/link")
    assert r.status_code == 200
    assert "Sign in with Google" in r.text


def test_login_redirects_to_google_with_state_cookie(client):
    r = client.get("/link/login")
    assert r.status_code == 302
    assert r.headers["location"].startswith("https://accounts.google.com/")
    assert link_app.OAUTH_STATE_COOKIE in r.headers.get("set-cookie", "")


def test_callback_error_param_is_escaped(client):
    r = client.get("/link/callback", params={"error": "<script>alert(1)</script>"})
    assert r.status_code == 400
    assert "<script>" not in r.text
    assert "&lt;script&gt;" in r.text


def test_callback_valid_state_without_cookie_rejected(client):
    state = link_app._sign("state", {"n": "abc"})
    r = client.get("/link/callback", params={"code": "x", "state": state})
    assert r.status_code == 400  # state not bound to this browser -> login CSRF blocked


def test_callback_state_cookie_mismatch_rejected(client):
    state = link_app._sign("state", {"n": "abc"})
    client.cookies.set(link_app.OAUTH_STATE_COOKIE, "different")
    r = client.get("/link/callback", params={"code": "x", "state": state})
    assert r.status_code == 400


def test_save_without_session_redirects(client):
    r = client.post("/link/save", data={"pat": "x", "csrf": "y"})
    assert r.status_code == 302


def test_save_bad_csrf_rejected(client):
    client.cookies.set(link_app.SESSION_COOKIE, _session("a@slogin.io"))
    r = client.post("/link/save", data={"pat": "plane_api_x", "csrf": "bogus"})
    assert r.status_code == 400


def test_index_with_session_shows_form(client):
    client.cookies.set(link_app.SESSION_COOKIE, _session("a@slogin.io"))
    r = client.get("/link")
    assert r.status_code == 200
    assert "Plane API token" in r.text


def test_delete_removes_registration(client, store):
    email = "a@slogin.io"
    asyncio.run(store.set_pat(email, "plane_api_x"))
    client.cookies.set(link_app.SESSION_COOKIE, _session(email))
    csrf = link_app._sign("csrf", {"email": email})
    r = client.post("/link/delete", data={"csrf": csrf})
    assert r.status_code == 200
    assert asyncio.run(store.has_pat(email)) is False
