"""Multi-workspace support for the unified WorkOS-OAuth endpoint.

The unified endpoint (`/connect/mcp`) lets the caller target any workspace they
belong to, per tool call. We do this the official FastMCP way — **tool
transformation** (`Tool.from_tool(..., transform_fn=...)` + `forward()`): every tool
gains a `workspace_slug` argument whose value is pushed into a contextvar that
`get_plane_client_context()` reads, then the rest of the args are forwarded to the
original tool unchanged. A `list_my_workspaces` tool lets the model discover which
slugs the user can use (resolved dynamically from Plane's DB — see
``workspace_directory``).
"""

from __future__ import annotations

from typing import Annotated, Any

from fastmcp import FastMCP
from fastmcp.server.dependencies import get_access_token
from fastmcp.tools import Tool
from fastmcp.tools.tool_transform import forward
from fastmcp.utilities.logging import get_logger
from pydantic import Field

from plane_mcp.client import request_workspace
from plane_mcp.workspace_directory import list_user_workspaces

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
    """Add a tool that lists the workspaces the signed-in user belongs to.

    Resolved dynamically from Plane's DB by the authenticated email, so newly
    created workspaces appear automatically with no config to maintain.
    """

    @mcp.tool()
    async def list_my_workspaces() -> list[dict[str, str]]:
        """List the Plane workspaces you belong to.

        Use the returned ``slug`` as the ``workspace_slug`` argument on other tools.
        """
        access = get_access_token()
        email = str((access.claims.get("email") if access else "") or "")
        return await list_user_workspaces(email)
