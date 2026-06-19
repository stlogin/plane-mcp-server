"""Tests for the read-only workspace directory lookup (graceful degradation)."""

import asyncio

from plane_mcp import workspace_directory


def test_empty_email_returns_empty():
    assert asyncio.run(workspace_directory.list_user_workspaces("")) == []


def test_no_dsn_returns_empty(monkeypatch):
    monkeypatch.delenv("PLANE_READONLY_DB_URL", raising=False)
    assert asyncio.run(workspace_directory.list_user_workspaces("ryo@slogin.io")) == []


def test_db_error_degrades_to_empty(monkeypatch):
    # DSN set but unreachable -> must return [] (never raise into the request).
    monkeypatch.setenv("PLANE_READONLY_DB_URL", "postgresql://x:y@127.0.0.1:1/none")
    assert asyncio.run(workspace_directory.list_user_workspaces("ryo@slogin.io")) == []
