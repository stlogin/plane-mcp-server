"""Multi-workspace support for the unified WorkOS-OAuth endpoint.

The per-workspace endpoints (`/mcp-oauth/<slug>`) bind one workspace via the URL.
The unified endpoint (`/mcp-all`) instead lets the caller target any workspace they
belong to, per tool call. We do this the official FastMCP way — **tool
transformation** (`Tool.from_tool(..., transform_fn=...)` + `forward()`): every tool
gains a `workspace_slug` argument whose value is pushed into a contextvar that
`get_plane_client_context()` reads, then the rest of the args are forwarded to the
original tool unchanged. A `list_my_workspaces` tool lets the model discover which
slugs the user can use.
"""

from __future__ import annotations

import os
from typing import Annotated, Any

import httpx
from fastmcp import FastMCP
from fastmcp.server.dependencies import get_access_token
from fastmcp.tools import Tool
from fastmcp.tools.tool_transform import forward
from fastmcp.utilities.logging import get_logger
from pydantic import Field

from plane_mcp.client import request_workspace

logger = get_logger(__name__)


async def _with_workspace(
    workspace_slug: Annotated[
        str | None,
        Field(description="Target Plane workspace slug. Call list_my_workspaces to see your options."),
    ] = None,
    **kwargs: Any,
) -> Any:
    """Transform wrapper: set the per-call workspace, then forward to the parent tool."""
    token = request_workspace.set(workspace_slug or None)
    try:
        return await forward(**kwargs)
    finally:
        request_workspace.reset(token)


def apply_workspace_arg(mcp: FastMCP) -> None:
    """Replace every registered tool with a transformed version that adds `workspace_slug`."""
    tools = [c for c in mcp.local_provider._components.values() if isinstance(c, Tool)]
    for tool in tools:
        transformed = Tool.from_tool(tool, transform_fn=_with_workspace)
        mcp.local_provider.remove_tool(tool.name)
        mcp.add_tool(transformed)
    logger.info("Applied per-call workspace_slug to %d tools", len(tools))


def register_list_my_workspaces(mcp: FastMCP) -> None:
    """Add a tool that lists the workspaces the caller's token can access.

    Plane's external API has no "list my workspaces" endpoint, so we probe each
    configured candidate (`WORKOS_WORKSPACES`) with the caller's token: a 200 on the
    members endpoint means they're a member.
    """

    @mcp.tool()
    async def list_my_workspaces() -> list[dict[str, str]]:
        """List the Plane workspaces you can access with your linked token.

        Use the returned ``slug`` as the ``workspace_slug`` argument on other tools.
        """
        access = get_access_token()
        api_key = access.token if access else os.getenv("PLANE_API_KEY", "")
        base = (os.getenv("PLANE_INTERNAL_BASE_URL") or os.getenv("PLANE_BASE_URL", "")).rstrip("/")
        candidates = [s.strip() for s in os.getenv("WORKOS_WORKSPACES", "").split(",") if s.strip()]

        accessible: list[dict[str, str]] = []
        async with httpx.AsyncClient(timeout=10, base_url=base) as client:
            for slug in candidates:
                try:
                    resp = await client.get(
                        f"/api/v1/workspaces/{slug}/members/", headers={"X-API-Key": api_key}
                    )
                except httpx.RequestError:
                    continue
                if resp.status_code == 200:
                    accessible.append({"slug": slug})
        return accessible
