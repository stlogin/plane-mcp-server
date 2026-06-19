"""Tests for per-user PAT resolution in the WorkOS verifier.

The parent ``WorkOSTokenVerifier.verify_token`` (which calls the WorkOS userinfo
endpoint over the network) is patched to return a crafted identity, so we test only
our added logic: domain / verified gating and per-user-vs-shared PAT selection.
"""

import asyncio

import pytest
from cryptography.fernet import Fernet
from fastmcp.server.auth import AccessToken
from fastmcp.server.auth.providers.workos import WorkOSTokenVerifier

import plane_mcp.user_pat_store as ups
from plane_mcp.auth.workos_auth_provider import SloginWorkOSVerifier
from plane_mcp.user_pat_store import UserPatStore


@pytest.fixture
def store(tmp_path, monkeypatch):
    s = UserPatStore(db_path=str(tmp_path / "pat.db"), enc_key=Fernet.generate_key().decode())
    monkeypatch.setattr(ups, "_store", s)
    return s


def _verifier(per_user_required: bool = True) -> SloginWorkOSVerifier:
    return SloginWorkOSVerifier(
        authkit_domain="https://test.authkit.app",
        workspace_slug="sli-dev",
        plane_api_key="shared_pat",
        allowed_email_domain="slogin.io",
        per_user_required=per_user_required,
    )


def _patch_identity(monkeypatch, claims: dict | None):
    async def fake(self, token):  # noqa: ANN001
        if claims is None:
            return None
        return AccessToken(token="workos_tok", client_id="c", scopes=[], expires_at=None, claims=claims)

    monkeypatch.setattr(WorkOSTokenVerifier, "verify_token", fake)


def test_registered_user_gets_own_pat(store, monkeypatch):
    asyncio.run(store.set_pat("ryo@slogin.io", "ryo_pat"))
    _patch_identity(monkeypatch, {"email": "ryo@slogin.io", "email_verified": True, "sub": "u1"})
    res = asyncio.run(_verifier().verify_token("t"))
    assert res is not None
    assert res.token == "ryo_pat"
    assert res.claims["auth_method"] == "api_key_env"
    assert res.claims["workspace_slug"] == "sli-dev"


def test_unregistered_required_is_rejected(store, monkeypatch):
    _patch_identity(monkeypatch, {"email": "new@slogin.io", "email_verified": True, "sub": "u2"})
    assert asyncio.run(_verifier(per_user_required=True).verify_token("t")) is None


def test_unregistered_falls_back_to_shared_when_not_required(store, monkeypatch):
    _patch_identity(monkeypatch, {"email": "new@slogin.io", "email_verified": True, "sub": "u2"})
    res = asyncio.run(_verifier(per_user_required=False).verify_token("t"))
    assert res is not None
    assert res.token == "shared_pat"


def test_unverified_email_rejected(store, monkeypatch):
    asyncio.run(store.set_pat("ryo@slogin.io", "ryo_pat"))
    _patch_identity(monkeypatch, {"email": "ryo@slogin.io", "email_verified": False, "sub": "u1"})
    assert asyncio.run(_verifier().verify_token("t")) is None


def test_wrong_domain_rejected(store, monkeypatch):
    asyncio.run(store.set_pat("x@gmail.com", "p"))
    _patch_identity(monkeypatch, {"email": "x@gmail.com", "email_verified": True, "sub": "u3"})
    assert asyncio.run(_verifier().verify_token("t")) is None


def test_parent_rejection_propagates(store, monkeypatch):
    _patch_identity(monkeypatch, None)
    assert asyncio.run(_verifier().verify_token("t")) is None
