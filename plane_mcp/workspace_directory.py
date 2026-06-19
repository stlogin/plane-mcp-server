"""Read-only lookup of a user's Plane workspaces, direct from Plane's Postgres.

Plane's external API has no "list my workspaces" endpoint, so the unified MCP
endpoint discovers a user's workspaces by querying Plane's DB (read-only) for the
active memberships of the authenticated email. New workspaces therefore appear
automatically — no config to maintain.

This is the one place the server touches the DB directly (everything else goes
through the Plane REST API). It is deliberately isolated here, uses a read-only
role (`PLANE_READONLY_DB_URL`), and **degrades gracefully**: any failure returns
an empty list so auth and all other tools are unaffected.
"""

from __future__ import annotations

import asyncio
import os

from fastmcp.utilities.logging import get_logger

logger = get_logger(__name__)

# Plane CE v1.3.1 schema (frozen fork). Active memberships of the given email.
_QUERY = """
SELECT w.slug, w.name
FROM workspaces w
JOIN workspace_members wm ON wm.workspace_id = w.id
JOIN users u ON u.id = wm.member_id
WHERE lower(u.email) = lower(%s)
  AND w.deleted_at IS NULL
  AND wm.deleted_at IS NULL
  AND wm.is_active = true
ORDER BY w.slug
"""


def _query_sync(email: str) -> list[dict[str, str]]:
    dsn = os.getenv("PLANE_READONLY_DB_URL", "")
    if not dsn:
        logger.warning("PLANE_READONLY_DB_URL not set; list_my_workspaces returns empty")
        return []
    try:
        import psycopg  # local import: only needed on this path

        with psycopg.connect(dsn, connect_timeout=5) as conn:
            with conn.cursor() as cur:
                cur.execute(_QUERY, (email,))
                return [{"slug": row[0], "name": row[1]} for row in cur.fetchall()]
    except Exception as exc:
        # Never break the request over a directory lookup.
        logger.error("workspace directory query failed: %s", type(exc).__name__)
        return []


async def list_user_workspaces(email: str) -> list[dict[str, str]]:
    """Return ``[{slug, name}, …]`` for the workspaces this email actively belongs to."""
    if not email:
        return []
    return await asyncio.to_thread(_query_sync, email)
