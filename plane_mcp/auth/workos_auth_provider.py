"""WorkOS AuthKit auth for the OAuth transport used by claude.ai / the mobile app.

claude.ai custom connectors only speak OAuth (DCR/PKCE) and cannot send custom
headers, so the existing header/PAT path can't be used from mobile. This module
wraps FastMCP's ``AuthKitProvider`` (WorkOS AuthKit, DCR) with a custom token
verifier that:

1. enforces a verified email on an allowed domain (e.g. ``slogin.io``) — WorkOS
   can be set to Google-only but does NOT restrict the Google Workspace domain on
   its own, so we reject any non-matching / unverified ``email`` here;
2. resolves the caller's **own** Plane PAT (registered at ``/link`` and stored in
   ``user_pat_store``, keyed by email) and injects it into the claims, so
   ``client.py`` calls Plane *as that user* and Plane RBAC scopes the request to
   their workspaces. With ``PER_USER_PAT_REQUIRED=false`` an unregistered user
   falls back to the shared ``PLANE_API_KEY`` (rollout aid). The Google user is
   captured in logs via the ``sub`` claim.

The header/PAT and stdio transports are unaffected.
"""

from __future__ import annotations

import os

from fastmcp.server.auth import AccessToken
from fastmcp.server.auth.providers.workos import AuthKitProvider, WorkOSTokenVerifier
from fastmcp.utilities.logging import get_logger

logger = get_logger(__name__)


class SloginWorkOSVerifier(WorkOSTokenVerifier):
    """WorkOS token verifier that gates on email domain and injects the user's PAT.

    Extends the stock userinfo-based verifier (which reliably returns ``email``),
    then rewrites the resulting ``AccessToken`` to carry the caller's own Plane PAT
    (looked up by email) and the route's workspace, so downstream calls run as that
    user. Falls back to the shared PAT only when ``per_user_required`` is False.
    """

    def __init__(
        self,
        *,
        authkit_domain: str,
        workspace_slug: str | None,
        plane_api_key: str,
        allowed_email_domain: str,
        per_user_required: bool = True,
        required_scopes: list[str] | None = None,
    ) -> None:
        super().__init__(authkit_domain=authkit_domain, required_scopes=required_scopes)
        self._workspace_slug = workspace_slug
        self._plane_api_key = plane_api_key
        # When True, the user must have registered their own Plane PAT at /link;
        # unregistered users are rejected. When False, fall back to the shared PAT
        # (eases rollout). Per-user PAT = the request calls Plane as the user, so
        # Plane RBAC filters by workspace membership.
        self._per_user_required = per_user_required
        # normalise "slogin.io" / "@slogin.io" -> "slogin.io"
        self._allowed_domain = (allowed_email_domain or "").strip().lower().lstrip("@")

    async def verify_token(self, token: str) -> AccessToken | None:
        access = await super().verify_token(token)
        if access is None:
            return None

        email = str(access.claims.get("email") or "").strip().lower()
        # Reject unverified emails: with Google-only auth this is always true, but it
        # blocks anyone who might register an @slogin.io address via another method
        # without controlling the inbox.
        if not access.claims.get("email_verified"):
            logger.warning(
                "WorkOS auth rejected: email not verified (email=%r sub=%s)",
                email,
                access.claims.get("sub"),
            )
            return None
        if self._allowed_domain and not email.endswith(f"@{self._allowed_domain}"):
            # email empty here usually means the access token lacked the "email"
            # scope, so WorkOS userinfo returned no email — see scopes_supported below.
            logger.warning(
                "WorkOS auth rejected: email=%r not in @%s (sub=%s)",
                email,
                self._allowed_domain,
                access.claims.get("sub"),
            )
            return None

        # Resolve which Plane PAT to call with. Prefer the user's own registered PAT
        # (so Plane RBAC scopes the request to that user's workspaces); fall back to
        # the shared PAT only when per-user is not required.
        from plane_mcp.user_pat_store import get_user_pat_store

        plane_token = await get_user_pat_store().get_pat(email)
        if plane_token is None:
            if self._per_user_required:
                logger.warning(
                    "WorkOS auth rejected: no Plane PAT registered for %s — register at /link (sub=%s)",
                    email,
                    access.claims.get("sub"),
                )
                return None
            plane_token = self._plane_api_key
            logger.info("No per-user PAT for %s; falling back to shared PAT", email)

        # Inject Plane credentials. client.py uses claims["auth_method"] to pick
        # the api_key path, .token as the api key, and claims["workspace_slug"].
        claims = dict(access.claims)
        claims["auth_method"] = "api_key_env"
        # Empty on the unified endpoint (workspace_slug=None) — workspace is then
        # supplied per tool call via the workspace_slug argument.
        claims["workspace_slug"] = self._workspace_slug or ""
        return AccessToken(
            token=plane_token,
            client_id=access.client_id,
            scopes=access.scopes,
            expires_at=access.expires_at,
            claims=claims,
        )


def build_workos_provider(*, workspace_slug: str | None, base_url: str) -> AuthKitProvider:
    """Build an AuthKitProvider for the OAuth transport.

    ``workspace_slug`` pins a single workspace (per-workspace endpoint); pass ``None``
    for the unified endpoint, where the workspace is chosen per tool call.

    Env:
      WORKOS_AUTHKIT_DOMAIN   e.g. https://<proj>.authkit.app  (required)
      WORKOS_CLIENT_ID        client_01...  (recommended: binds JWT audience)
      ALLOWED_EMAIL_DOMAIN    default "slogin.io"
      PER_USER_PAT_REQUIRED   default "true"; when false, fall back to the shared PAT
      PLANE_API_KEY           shared fallback PAT (only used when per-user not required)
    """
    authkit_domain = os.getenv("WORKOS_AUTHKIT_DOMAIN", "")
    if not authkit_domain:
        raise ValueError("WORKOS_AUTHKIT_DOMAIN is not set")

    per_user_required = os.getenv("PER_USER_PAT_REQUIRED", "true").strip().lower() in {"1", "true", "yes", "on"}
    verifier = SloginWorkOSVerifier(
        authkit_domain=authkit_domain,
        workspace_slug=workspace_slug,
        plane_api_key=os.getenv("PLANE_API_KEY", ""),
        allowed_email_domain=os.getenv("ALLOWED_EMAIL_DOMAIN", "slogin.io"),
        per_user_required=per_user_required,
        required_scopes=None,
    )
    return AuthKitProvider(
        authkit_domain=authkit_domain,
        base_url=base_url,
        client_id=os.getenv("WORKOS_CLIENT_ID") or None,
        token_verifier=verifier,
        # Advertise the OIDC scopes so the client (claude.ai) requests them; without
        # "email"/"profile" the WorkOS userinfo response omits the email and the
        # domain check above can never pass. Our verifier doesn't enforce scopes.
        scopes_supported=["openid", "email", "profile"],
        resource_name="Plane MCP Server",
    )
