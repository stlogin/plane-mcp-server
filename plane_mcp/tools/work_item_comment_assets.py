"""Comment asset tools for Plane MCP Server (comment-embedded images/files).

These tools access assets embedded in comment HTML via the workspace asset API
(GET /api/v1/workspaces/{slug}/assets/{id}/). CE v1.3.1 ships with a bug where
this endpoint returns 500 due to an S3Storage constructor mismatch; apply the
one-line storage.py patch (is_server=False default) before using these tools.
"""

import mimetypes
import re
from typing import Any

import requests as _requests
from fastmcp import FastMCP
from fastmcp.utilities.types import Image

from plane_mcp.client import get_plane_client_context

_UUID_RE = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_HTTP_TIMEOUT = (10, 60)
_IMAGE_READ_LIMIT = 5 * 1024 * 1024  # 5 MB
_TEXT_READ_LIMIT = 1 * 1024 * 1024  # 1 MB

_READABLE_IMAGE_TYPES: frozenset[str] = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"})
_READABLE_TEXT_TYPES: frozenset[str] = frozenset(
    {
        "text/plain",
        "text/markdown",
        "text/csv",
        "text/html",
        "text/xml",
        "text/yaml",
        "application/json",
        "application/xml",
        "application/yaml",
        "application/x-yaml",
    }
)


def _http_session(client) -> tuple[str, dict[str, str]]:
    """Return (base_url, auth_headers) derived from a PlaneClient instance."""
    # config.base_path is "{base_url}/api/v1"
    base_url = client.config.base_path.removesuffix("/api/v1")
    if client.config.api_key:
        headers = {"X-API-Key": client.config.api_key}
    else:
        headers = {"Authorization": f"Bearer {client.config.access_token}"}
    return base_url, headers


def _parse_embedded_assets(html: str) -> list[dict[str, str]]:
    """Extract asset UUIDs from comment HTML."""
    seen: set[str] = set()
    out: list[dict[str, str]] = []
    for m in re.finditer(rf'<image-component\b[^>]*\bsrc="({_UUID_RE})"[^>]*>', html or ""):
        aid = m.group(1)
        if aid not in seen:
            seen.add(aid)
            out.append({"asset_id": aid, "kind": "image"})
    for m in re.finditer(rf'<(?!image-component)[a-z-]+\b[^>]*\bsrc="({_UUID_RE})"[^>]*>', html or ""):
        aid = m.group(1)
        if aid not in seen:
            seen.add(aid)
            out.append({"asset_id": aid, "kind": "file"})
    return out


def _strip_html(html: str) -> str:
    return re.sub(r"<[^>]+>", "", html or "").strip()


def register_work_item_comment_asset_tools(mcp: FastMCP) -> None:
    """Register work item comment asset tools with the MCP server."""

    @mcp.tool()
    def list_work_item_comment_assets(
        project_id: str,
        work_item_id: str,
    ) -> list[dict[str, Any]]:
        """List assets (images / files) embedded inside a work item's comments.

        Comment-embedded assets appear inline in comment HTML as <image-component>
        or similar elements. They are distinct from work item attachments. Use
        get_work_item_comment_asset_url or read_work_item_comment_asset to access
        the actual content.

        Args:
            project_id: UUID of the project
            work_item_id: UUID of the work item

        Returns:
            List of dicts with: comment_id, comment_preview, asset_id, kind
            (image|file), asset_name, asset_type.
        """
        client, workspace_slug = get_plane_client_context()
        base_url, headers = _http_session(client)

        resp = _requests.get(
            f"{base_url}/api/v1/workspaces/{workspace_slug}/projects/{project_id}/issues/{work_item_id}/comments/",
            headers=headers,
            timeout=_HTTP_TIMEOUT,
        )
        try:
            resp.raise_for_status()
        except _requests.HTTPError as e:
            raise ValueError(f"Failed to list comments: HTTP {resp.status_code}") from e

        data = resp.json()
        comments = data.get("results", data) if isinstance(data, dict) else (data or [])

        results: list[dict[str, Any]] = []
        for comment in comments:
            html = comment.get("comment_html", "")
            assets = _parse_embedded_assets(html)
            if not assets:
                continue
            preview = _strip_html(html)[:80]
            for asset in assets:
                entry: dict[str, Any] = {
                    "comment_id": comment.get("id"),
                    "comment_preview": preview,
                    "asset_id": asset["asset_id"],
                    "kind": asset["kind"],
                    "asset_name": None,
                    "asset_type": None,
                }
                try:
                    meta = _requests.get(
                        f"{base_url}/api/v1/workspaces/{workspace_slug}/assets/{asset['asset_id']}/",
                        headers=headers,
                        timeout=(5, 10),
                    )
                    if meta.ok:
                        d = meta.json()
                        entry["asset_name"] = d.get("asset_name")
                        entry["asset_type"] = d.get("asset_type")
                except Exception:
                    pass
                results.append(entry)

        return results

    @mcp.tool()
    def get_work_item_comment_asset_url(
        asset_id: str,
    ) -> dict[str, Any]:
        """Get a presigned download URL for an asset embedded in a comment.

        Use list_work_item_comment_assets first to find asset IDs. The returned
        URL is typically valid for ~1 hour and requires no Plane authentication.

        Args:
            asset_id: UUID of the comment asset (from list_comment_assets)

        Returns:
            Dict with: asset_url (presigned), asset_name, asset_type.
        """
        client, workspace_slug = get_plane_client_context()
        base_url, headers = _http_session(client)

        resp = _requests.get(
            f"{base_url}/api/v1/workspaces/{workspace_slug}/assets/{asset_id}/",
            headers=headers,
            timeout=_HTTP_TIMEOUT,
        )
        try:
            resp.raise_for_status()
        except _requests.HTTPError as e:
            raise ValueError(f"Failed to fetch asset {asset_id!r}: HTTP {resp.status_code}") from e

        data = resp.json()
        asset_url = data.get("asset_url")
        if not asset_url:
            raise ValueError(
                f"No download URL returned for asset {asset_id!r}. "
                "Ensure the storage.py patch is applied on the Plane instance."
            )

        return {
            "asset_url": asset_url,
            "asset_name": data.get("asset_name"),
            "asset_type": data.get("asset_type"),
        }

    @mcp.tool()
    def read_work_item_comment_asset(
        asset_id: str,
    ) -> "Image | str":
        """Fetch a comment-embedded asset so the LLM can view or read it.

        Supported file types:
          Images (returned as vision-readable, max 5 MB): PNG, JPEG, GIF, WEBP
          Text (returned as string, max 1 MB): TXT, MD, CSV, HTML, XML, YAML, JSON

        For unsupported types (PDF, DOCX, etc.) use get_work_item_comment_asset_url
        instead to get a direct download link.

        Args:
            asset_id: UUID of the comment asset (from list_comment_assets)

        Returns:
            Image for image files, plain string for text files.
        """
        client, workspace_slug = get_plane_client_context()
        base_url, headers = _http_session(client)

        resp = _requests.get(
            f"{base_url}/api/v1/workspaces/{workspace_slug}/assets/{asset_id}/",
            headers=headers,
            timeout=_HTTP_TIMEOUT,
        )
        try:
            resp.raise_for_status()
        except _requests.HTTPError as e:
            raise ValueError(f"Failed to fetch asset {asset_id!r}: HTTP {resp.status_code}") from e

        data = resp.json()
        asset_url = data.get("asset_url")
        if not asset_url:
            raise ValueError(f"No download URL for asset {asset_id!r}.")

        asset_name = data.get("asset_name") or asset_id
        asset_type = data.get("asset_type") or ""
        if not asset_type or asset_type == "application/octet-stream":
            asset_type = mimetypes.guess_type(asset_name)[0] or "application/octet-stream"

        is_image = asset_type in _READABLE_IMAGE_TYPES
        is_text = asset_type in _READABLE_TEXT_TYPES

        if not is_image and not is_text:
            raise ValueError(
                f"Unsupported type {asset_type!r} for {asset_name!r}. "
                "Use get_work_item_comment_asset_url to get a direct download link instead."
            )

        try:
            file_resp = _requests.get(asset_url, timeout=_HTTP_TIMEOUT)
            file_resp.raise_for_status()
        except _requests.RequestException as e:
            raise ValueError(f"Failed to download asset content: {e}") from e

        file_bytes = file_resp.content
        size = len(file_bytes)

        if is_image:
            if size > _IMAGE_READ_LIMIT:
                raise ValueError(
                    f"Image {asset_name!r} is {size / 1024 / 1024:.1f} MB, "
                    f"exceeds {_IMAGE_READ_LIMIT // 1024 // 1024} MB limit. "
                    "Use get_work_item_comment_asset_url instead."
                )
            fmt = asset_type.removeprefix("image/")
            return Image(data=file_bytes, format=fmt)

        if size > _TEXT_READ_LIMIT:
            raise ValueError(
                f"Text {asset_name!r} is {size / 1024 / 1024:.1f} MB, "
                f"exceeds {_TEXT_READ_LIMIT // 1024 // 1024} MB limit. "
                "Use get_work_item_comment_asset_url instead."
            )
        return file_bytes.decode("utf-8", errors="replace")
