"""Tests for the unified (all-workspaces) WorkOS endpoint tool transformation."""

import asyncio

import pytest

from plane_mcp.server import get_workos_unified_mcp


@pytest.fixture
def unified(monkeypatch):
    monkeypatch.setenv("WORKOS_AUTHKIT_DOMAIN", "https://test.authkit.app")
    return get_workos_unified_mcp("https://plane.slogin.io/mcp-all")


def _tools_by_name(mcp):
    tools = asyncio.run(mcp.list_tools(run_middleware=False))
    return {t.name: t for t in tools}


def test_tools_gain_workspace_slug_and_keep_original_args(unified):
    by_name = _tools_by_name(unified)
    props = by_name["list_work_items"].parameters.get("properties", {})
    assert "workspace_slug" in props  # added by transformation
    assert "project_id" in props  # original arg preserved


def test_list_my_workspaces_present_without_workspace_slug(unified):
    by_name = _tools_by_name(unified)
    assert "list_my_workspaces" in by_name
    # the helper is added after the transform, so it has no workspace_slug arg
    assert "workspace_slug" not in by_name["list_my_workspaces"].parameters.get("properties", {})


def test_all_domain_tools_have_workspace_slug(unified):
    by_name = _tools_by_name(unified)
    # every tool except the discovery helper should accept workspace_slug
    missing = [
        name
        for name, t in by_name.items()
        if name != "list_my_workspaces" and "workspace_slug" not in t.parameters.get("properties", {})
    ]
    assert missing == []
