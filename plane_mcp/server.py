"""FastMCP server factories for the three supported transports."""

from __future__ import annotations

import os

from fastmcp import FastMCP
from fastmcp.server.middleware.logging import StructuredLoggingMiddleware
from mcp.types import Icon

from plane_mcp.auth import PlaneHeaderAuthProvider, PlaneOAuthProvider
from plane_mcp.instructions import SERVER_INSTRUCTIONS
from plane_mcp.storage import build_token_store
from plane_mcp.tools import register_tools


def get_oauth_mcp(base_path: str = "/") -> FastMCP:
    """Build the FastMCP instance for the OAuth HTTP / SSE transports."""
    oauth_mcp = FastMCP(
        "Plane MCP Server",
        instructions=SERVER_INSTRUCTIONS,
        icons=[Icon(src="https://plane.so/favicon.ico", alt="Plane MCP Server")],
        website_url="https://plane.so",
        auth=PlaneOAuthProvider(
            client_id=os.getenv("PLANE_OAUTH_PROVIDER_CLIENT_ID", ""),
            client_secret=os.getenv("PLANE_OAUTH_PROVIDER_CLIENT_SECRET", ""),
            base_url=f"{os.getenv('PLANE_OAUTH_PROVIDER_BASE_URL')}{base_path}",
            plane_base_url=os.getenv("PLANE_BASE_URL", ""),
            plane_internal_base_url=os.getenv("PLANE_INTERNAL_BASE_URL", ""),
            enable_cimd=os.getenv("PLANE_OAUTH_PROVIDER_ENABLE_CIMD", "false").lower() == "true",
            client_storage=build_token_store(),
            required_scopes=["read", "write"],
            allowed_client_redirect_uris=[
                # Localhost only for http (dynamic ports from MCP clients)
                "http://localhost:*",
                "http://localhost:*/*",
                "http://127.0.0.1:*",
                "http://127.0.0.1:*/*",
                # Known MCP client custom protocol schemes
                "cursor://*",
                "vscode://*",
                "vscode-insiders://*",
                "windsurf://*",
                "claude://*",
                # Claude.ai web client
                "https://claude.ai/*",
            ],
        ),
    )
    oauth_mcp.add_middleware(StructuredLoggingMiddleware(include_payloads=True))
    register_tools(oauth_mcp)
    return oauth_mcp


def get_header_mcp():
    header_mcp = FastMCP(
        "Plane MCP Server (header-http)",
        instructions=SERVER_INSTRUCTIONS,
        auth=PlaneHeaderAuthProvider(
            required_scopes=["read", "write"],
        ),
    )
    header_mcp.add_middleware(StructuredLoggingMiddleware(include_payloads=True))
    register_tools(header_mcp)
    return header_mcp


def get_workos_mcp(workspace_slug: str, base_url: str) -> FastMCP:
    """Build the FastMCP instance for the WorkOS-OAuth transport (claude.ai / mobile).

    One instance per workspace (claude.ai cannot send the x-workspace-slug header).
    The WorkOS provider verifies the user (Google / slogin.io) and injects a
    server-side Plane PAT + this workspace into the request claims.
    """
    from plane_mcp.auth.workos_auth_provider import build_workos_provider

    workos_mcp = FastMCP(
        "Plane MCP Server (WorkOS)",
        instructions=SERVER_INSTRUCTIONS,
        icons=[Icon(src="https://plane.so/favicon.ico", alt="Plane MCP Server")],
        website_url="https://plane.so",
        auth=build_workos_provider(workspace_slug=workspace_slug, base_url=base_url),
    )
    workos_mcp.add_middleware(StructuredLoggingMiddleware(include_payloads=True))
    register_tools(workos_mcp)
    return workos_mcp


def get_workos_unified_mcp(base_url: str) -> FastMCP:
    """Build the WorkOS-OAuth instance that spans all of the user's workspaces (one URL).

    Unlike get_workos_mcp (one workspace per URL), here the workspace is chosen
    per tool call: every tool gains a ``workspace_slug`` argument (FastMCP tool
    transformation), plus a ``list_my_workspaces`` tool. The user's own PAT is used,
    so Plane RBAC limits results to the workspaces they belong to.
    """
    from plane_mcp.auth.workos_auth_provider import build_workos_provider
    from plane_mcp.tools.multi_workspace import apply_workspace_arg, register_list_my_workspaces

    mcp = FastMCP(
        "Plane MCP Server (WorkOS, all workspaces)",
        instructions=SERVER_INSTRUCTIONS,
        icons=[Icon(src="https://plane.so/favicon.ico", alt="Plane MCP Server")],
        website_url="https://plane.so",
        auth=build_workos_provider(workspace_slug=None, base_url=base_url),
    )
    mcp.add_middleware(StructuredLoggingMiddleware(include_payloads=True))
    register_tools(mcp)
    apply_workspace_arg(mcp)
    register_list_my_workspaces(mcp)
    return mcp


def get_stdio_mcp():
    stdio_mcp = FastMCP(
        "Plane MCP Server (stdio)",
        instructions=SERVER_INSTRUCTIONS,
    )
    stdio_mcp.add_middleware(StructuredLoggingMiddleware(include_payloads=True))
    register_tools(stdio_mcp)
    return stdio_mcp
