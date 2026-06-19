"""Main entry point for the Plane MCP Server."""

import json
import logging
import os
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from enum import Enum

import uvicorn
from fastmcp.server.dependencies import get_access_token
from starlette.applications import Starlette
from starlette.middleware.cors import CORSMiddleware
from starlette.routing import Mount

from plane_mcp.server import (
    get_header_mcp,
    get_oauth_mcp,
    get_stdio_mcp,
    get_workos_mcp,
    get_workos_unified_mcp,
)


class UserContextFilter(logging.Filter):
    """Attach the authenticated user's id to every log record.

    Pulls the current request's access token via FastMCP's dependency, which
    returns None (never raises) outside a request context — so startup logs and
    stdio mode simply carry no user info. Only the opaque user id is recorded;
    PII such as the display name / email is intentionally never logged.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        user_id = None
        try:
            token = get_access_token()
            if token:
                user_id = token.claims.get("sub")
        except Exception as exc:
            # Never let logging enrichment break a request, but leave a signal.
            record.user_context_enrichment_error = type(exc).__name__
        record.user_id = user_id
        return True


class JSONFormatter(logging.Formatter):
    """JSON log formatter for structured logging (Datadog, ELK, etc.)."""

    def format(self, record: logging.LogRecord) -> str:
        log_entry = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        user_id = getattr(record, "user_id", None)
        if user_id:
            log_entry["user_id"] = user_id
        err = getattr(record, "user_context_enrichment_error", None)
        if err:
            log_entry["user_context_enrichment_error"] = err
        if record.exc_info and record.exc_info[1]:
            log_entry["error"] = {
                "type": type(record.exc_info[1]).__name__,
                "message": str(record.exc_info[1]),
            }
        return json.dumps(log_entry)


def configure_json_logging():
    """Replace FastMCP's Rich handlers with a JSON formatter on the fastmcp logger."""
    fastmcp_logger = logging.getLogger("fastmcp")

    # Remove all existing handlers (Rich)
    for handler in fastmcp_logger.handlers[:]:
        fastmcp_logger.removeHandler(handler)

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JSONFormatter())
    handler.addFilter(UserContextFilter())
    fastmcp_logger.addHandler(handler)
    fastmcp_logger.setLevel(logging.INFO)
    fastmcp_logger.propagate = False


configure_json_logging()

logger = logging.getLogger("fastmcp.plane_mcp")


class ServerMode(Enum):
    STDIO = "stdio"
    SSE = "sse"
    HTTP = "http"
    HEADER = "header"  # header-auth HTTP only — no OAuth required (self-hosted)
    WORKOS = "workos"  # header-auth PAT + WorkOS-OAuth (claude.ai / mobile) side by side


@asynccontextmanager
async def combined_lifespan(oauth_app, header_app, sse_app):
    """Combine lifespans from both OAuth and Header MCP apps."""
    # Start both lifespans
    async with oauth_app.lifespan(oauth_app):
        async with header_app.lifespan(header_app):
            async with sse_app.lifespan(sse_app):
                yield


def make_combined_lifespan(apps):
    """Combine the lifespans of an arbitrary list of Starlette sub-apps."""
    from contextlib import AsyncExitStack

    @asynccontextmanager
    async def _lifespan(app):
        async with AsyncExitStack() as stack:
            for sub in apps:
                await stack.enter_async_context(sub.lifespan(sub))
            yield

    return lambda app: _lifespan(app)


def configure_uvicorn_json_logging() -> None:
    """Route uvicorn loggers through the JSON formatter + user-context filter."""
    for uv_logger_name in ("uvicorn", "uvicorn.error"):
        uv_logger = logging.getLogger(uv_logger_name)
        for h in uv_logger.handlers[:]:
            uv_logger.removeHandler(h)
        uv_handler = logging.StreamHandler(sys.stderr)
        uv_handler.setFormatter(JSONFormatter())
        uv_handler.addFilter(UserContextFilter())
        uv_logger.addHandler(uv_handler)


def main() -> None:
    """Run the MCP server."""
    server_mode = ServerMode.STDIO
    if len(sys.argv) > 1:
        server_mode = ServerMode(sys.argv[1])

    if server_mode == ServerMode.STDIO:
        # Validate API_KEY and PLANE_WORKSPACE_SLUG are set
        if not os.getenv("PLANE_API_KEY"):
            raise ValueError("PLANE_API_KEY is not set")
        if not os.getenv("PLANE_WORKSPACE_SLUG"):
            raise ValueError("PLANE_WORKSPACE_SLUG is not set")

        get_stdio_mcp().run()
        return

    if server_mode == ServerMode.HTTP:
        prefix = os.getenv("MCP_PATH_PREFIX") or ""

        oauth_mcp = get_oauth_mcp(prefix + "/http")
        oauth_app = oauth_mcp.http_app(stateless_http=True)
        header_app = get_header_mcp().http_app(stateless_http=True)

        sse_mcp = get_oauth_mcp(prefix)
        sse_app = sse_mcp.http_app(transport="sse")

        # mcp_path is appended to the auth provider's base_url to form the
        # advertised resource URL. base_url already carries the prefix, so these
        # stay at /mcp and /sse to avoid double-prefixing.
        oauth_well_known = oauth_mcp.auth.get_well_known_routes(mcp_path="/mcp")
        sse_well_known = sse_mcp.auth.get_well_known_routes(mcp_path="/sse")

        app = Starlette(
            routes=[
                # Well-known routes for OAuth and Header HTTP
                *oauth_well_known,
                *sse_well_known,
                # Mount both MCP servers
                Mount(prefix + "/http/api-key", app=header_app),
                Mount(prefix + "/http", app=oauth_app),
                Mount(prefix or "/", app=sse_app),
            ],
            lifespan=lambda app: combined_lifespan(oauth_app, header_app, sse_app),
        )

        app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_credentials=False,
            allow_methods=["*"],
            allow_headers=["*"],
        )

        # Configure uvicorn loggers to use JSON formatting too
        for uv_logger_name in ("uvicorn", "uvicorn.error"):
            uv_logger = logging.getLogger(uv_logger_name)
            for h in uv_logger.handlers[:]:
                uv_logger.removeHandler(h)
            uv_handler = logging.StreamHandler(sys.stderr)
            uv_handler.setFormatter(JSONFormatter())
            uv_handler.addFilter(UserContextFilter())
            uv_logger.addHandler(uv_handler)

        logger.info("Starting HTTP server at URLs: /mcp and /header/mcp")
        uvicorn.run(
            app,
            host="0.0.0.0",
            port=8211,
            log_level="info",
            access_log=False,
        )
        return

    if server_mode == ServerMode.WORKOS:
        # Production self-hosted mode: serve the header/PAT endpoint (Claude Code, at
        # /mcp) and one WorkOS-OAuth endpoint per workspace (claude.ai / mobile, at
        # /mcp-oauth/<slug>/mcp) from the same process. Caddy forwards /mcp* to here,
        # so /mcp-oauth/* needs no proxy change.
        public_base = (os.getenv("PUBLIC_BASE_URL") or "").rstrip("/")
        if not public_base:
            raise ValueError("PUBLIC_BASE_URL is not set (e.g. https://plane.slogin.io)")
        workspaces = [w.strip() for w in (os.getenv("WORKOS_WORKSPACES") or "").split(",") if w.strip()]
        if not workspaces:
            raise ValueError("WORKOS_WORKSPACES is not set (comma-separated workspace slugs)")

        header_app = get_header_mcp().http_app(stateless_http=True)

        lifespan_apps = [header_app]
        well_known_routes: list = []
        seen_well_known: set[str] = set()
        oauth_mounts: list = []
        for slug in workspaces:
            workos_mcp = get_workos_mcp(slug, f"{public_base}/mcp-oauth/{slug}")
            workos_app = workos_mcp.http_app(stateless_http=True)
            lifespan_apps.append(workos_app)
            # base_url already carries the /mcp-oauth/<slug> prefix, so the resource
            # URL is …/mcp-oauth/<slug>/mcp and the well-known paths are unique per
            # workspace. The shared /.well-known/oauth-authorization-server route is
            # identical for every workspace, so register it only once.
            for route in workos_mcp.auth.get_well_known_routes(mcp_path="/mcp"):
                if route.path in seen_well_known:
                    continue
                seen_well_known.add(route.path)
                well_known_routes.append(route)
            oauth_mounts.append(Mount(f"/mcp-oauth/{slug}", app=workos_app))

        # Unified endpoint: one URL spanning all of the user's workspaces. Workspace
        # is chosen per tool call (workspace_slug arg); the user's own PAT scopes it.
        # Served at /connect/mcp (Caddy routes /connect* to this container).
        unified_mcp = get_workos_unified_mcp(f"{public_base}/connect")
        unified_app = unified_mcp.http_app(stateless_http=True)
        lifespan_apps.append(unified_app)
        for route in unified_mcp.auth.get_well_known_routes(mcp_path="/mcp"):
            if route.path in seen_well_known:
                continue
            seen_well_known.add(route.path)
            well_known_routes.append(route)
        oauth_mounts.append(Mount("/connect", app=unified_app))

        from plane_mcp.link_app import get_link_routes

        app = Starlette(
            routes=[
                *well_known_routes,
                # /link self-service: users bind their own Plane PAT to their email.
                *get_link_routes(),
                *oauth_mounts,
                # Header/PAT endpoint last so it acts as the /mcp catch-all.
                Mount("/", app=header_app),
            ],
            lifespan=make_combined_lifespan(lifespan_apps),
        )
        app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_credentials=False,
            allow_methods=["*"],
            allow_headers=["*"],
        )
        configure_uvicorn_json_logging()
        logger.info(
            "Starting WORKOS server: header /mcp + WorkOS OAuth /mcp-oauth/<slug> for %s",
            ",".join(workspaces),
        )
        uvicorn.run(
            app,
            host="0.0.0.0",
            port=int(os.getenv("FASTMCP_PORT", "8211")),
            log_level="info",
            access_log=False,
        )
        return

    if server_mode == ServerMode.HEADER:
        header_app = get_header_mcp().http_app(stateless_http=True)
        app = Starlette(
            routes=[Mount("/", app=header_app)],
            lifespan=header_app.lifespan,
        )
        app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_credentials=False,
            allow_methods=["*"],
            allow_headers=["*"],
        )
        for uv_logger_name in ("uvicorn", "uvicorn.error"):
            uv_logger = logging.getLogger(uv_logger_name)
            for h in uv_logger.handlers[:]:
                uv_logger.removeHandler(h)
            uv_handler = logging.StreamHandler(sys.stderr)
            uv_handler.setFormatter(JSONFormatter())
            uv_handler.addFilter(UserContextFilter())
            uv_logger.addHandler(uv_handler)
        logger.info("Starting header-auth HTTP server (no OAuth)")
        uvicorn.run(
            app,
            host="0.0.0.0",
            port=int(os.getenv("FASTMCP_PORT", "8211")),
            log_level="info",
            access_log=False,
        )
        return


if __name__ == "__main__":
    main()
