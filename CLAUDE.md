# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Plane MCP Server — a Python-based Model Context Protocol server that exposes Plane's project management API as MCP tools. Built on FastMCP with the official `plane-sdk`. Supports three transport modes: stdio (local), HTTP (with OAuth or header auth), and SSE (legacy).

## Common Commands

```bash
# Install dependencies (uses uv)
uv pip install -e ".[dev]"

# Run the server locally (stdio mode)
PLANE_API_KEY=... PLANE_WORKSPACE_SLUG=... python -m plane_mcp stdio

# Run HTTP server
python -m plane_mcp http

# Run all tests
pytest

# Run a single test
pytest tests/test_integration.py::test_full_integration -v

# Run tests with env vars from file
export $(cat .env.test.local | xargs) && pytest tests/ -v

# Format code (line length: 120)
ruff format plane_mcp/

# Lint (rules: E, F, I, UP, B; line length: 120)
ruff check plane_mcp/
```

## Architecture

### Entry Point & Transport Modes

`plane_mcp/__main__.py` parses a positional arg (`stdio`, `http`, `header`, `sse`, or `workos`) and launches the corresponding server:
- **stdio**: Requires `PLANE_API_KEY` + `PLANE_WORKSPACE_SLUG` env vars. Runs locally.
- **header**: header-auth HTTP only (`x-api-key`/Bearer + `x-workspace-slug`); no OAuth.
- **workos** (production self-hosted, what runs on EC2): serves the **header/PAT** endpoint at `/mcp` (Claude Code) **and** the **WorkOS-OAuth** endpoint at `/connect/mcp` (claude.ai / mobile) plus the `/link` registration page, from one process.
- **http**: Plane-OAuth (`/oauth/mcp`) + header (`/http/api-key/mcp`) — cloud/legacy model.
- **sse**: Legacy OAuth-only SSE transport.

### Server Factories (`server.py`)

Factory functions (`get_oauth_mcp`, `get_header_mcp`, `get_stdio_mcp`, `get_workos_unified_mcp`) each create a `FastMCP` instance, register all tools, and configure the auth provider. `get_workos_unified_mcp` additionally applies the per-call `workspace_slug` transform and the `list_my_workspaces` tool (`tools/multi_workspace.py`).

### Client Context (`client.py`)

`get_plane_client_context()` returns a `PlaneClientContext(client, workspace_slug)` namedtuple. It resolves credentials from the MCP request context (OAuth token or header API key) or from environment variables (stdio mode). Prefers `PLANE_INTERNAL_BASE_URL` for server-to-server calls.

### Authentication (`auth/`)

- `PlaneOAuthProvider` — Full OAuth flow with token verification against the Plane API (cloud/legacy `http` mode).
- `PlaneHeaderAuthProvider` — Simple header-based auth using `x-api-key`/Bearer + `x-workspace-slug` headers (Claude Code).
- `SloginWorkOSVerifier` / `build_workos_provider` (`auth/workos_auth_provider.py`) — WorkOS AuthKit (Google, verified `@slogin.io`) for claude.ai / mobile. After verifying, it injects the user's **own** Plane PAT — registered at `/link` and stored encrypted in SQLite (`user_pat_store.py`, `link_app.py`) — so calls run as that user (Plane RBAC scopes them). Workspace is per tool call (`tools/multi_workspace.py` tool transformation); `list_my_workspaces` enumerates the user's workspaces with a read-only query against Plane's Postgres (`workspace_directory.py`). Gated by `PER_USER_PAT_REQUIRED`.

### Tools (`tools/`)

19 tool modules organized by Plane domain (projects, work_items, cycles, modules, etc.), totaling 55+ tools. Each module exports a `register_*_tools(mcp: FastMCP)` function called from `tools/__init__.py`.

**Tool pattern:**
```python
def register_*_tools(mcp: FastMCP) -> None:
    @mcp.tool()
    def tool_name(param: str, optional_param: str | None = None) -> SomePlaneModel:
        """Docstring with Args and Returns sections."""
        client, workspace_slug = get_plane_client_context()
        return client.endpoint.operation(workspace_slug=workspace_slug, ...)
```

Tools return Pydantic models from `plane-sdk` and use Python 3.10+ union syntax (`str | None`).

### Testing

Integration tests in `tests/test_integration.py` use `FastMCP.Client` with `StreamableHttpTransport`. Tests run against a live Plane instance — configure via `.env.test` (copy to `.env.test.local` with real values).

## Key Environment Variables

| Variable | Required For | Purpose |
|---|---|---|
| `PLANE_API_KEY` | stdio | API key for authentication |
| `PLANE_WORKSPACE_SLUG` | stdio | Target workspace |
| `PLANE_BASE_URL` | all (default: https://api.plane.so) | Plane API URL |
| `PLANE_INTERNAL_BASE_URL` | http/sse (optional) | Internal URL for server-to-server calls |
| `REDIS_HOST` / `REDIS_PORT` | http/sse (optional) | Token storage (falls back to in-memory) |
| `PLANE_OAUTH_PROVIDER_*` | http/sse OAuth | OAuth client credentials and base URL |
| `PUBLIC_BASE_URL` | workos | Public base (e.g. `https://plane.slogin.io`) for OAuth resource URLs |
| `WORKOS_AUTHKIT_DOMAIN` / `WORKOS_CLIENT_ID` | workos | WorkOS AuthKit domain + project client ID |
| `ALLOWED_EMAIL_DOMAIN` | workos (default `slogin.io`) | Allowed verified-email domain |
| `MCP_PAT_ENC_KEY` / `MCP_PAT_DB_PATH` | workos | Fernet key + SQLite path for the email→PAT store |
| `GOOGLE_OAUTH_CLIENT_ID` / `GOOGLE_OAUTH_CLIENT_SECRET` | workos | Google client for the `/link` sign-in (reuses Plane's) |
| `PER_USER_PAT_REQUIRED` | workos (default `true`) | Reject unregistered users; `false` falls back to `PLANE_API_KEY` |
| `PLANE_READONLY_DB_URL` | workos | Read-only DSN (`mcp_readonly`) for `list_my_workspaces` |
