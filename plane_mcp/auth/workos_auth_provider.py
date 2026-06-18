"""WorkOS AuthKit auth for the OAuth transport used by claude.ai / the mobile app.

claude.ai custom connectors only speak OAuth (DCR/PKCE) and cannot send custom
headers, so the existing header/PAT path can't be used from mobile. This module
wraps FastMCP's ``AuthKitProvider`` (WorkOS AuthKit, DCR) with a custom token
verifier that:

1. enforces an allowed email domain (e.g. ``slogin.io``) — WorkOS can be set to
   Google-only but does NOT restrict the Google Workspace domain on its own, so
   we reject any non-matching ``email`` here;
2. injects claims so ``client.py`` builds the PlaneClient with a server-side
   shared PAT and a fixed workspace. WorkOS authenticates the *human*; Plane is
   then called with our cross-user key (same model as the header path). The
   actual Google user is still captured in logs via the ``sub`` claim.

The header/PAT and stdio transports are unaffected.
"""

from __future__ import annotations

import os

from fastmcp.server.auth import AccessToken
from fastmcp.server.auth.providers.workos import AuthKitProvider, WorkOSTokenVerifier
from fastmcp.utilities.logging import get_logger

logger = get_logger(__name__)


class SloginWorkOSVerifier(WorkOSTokenVerifier):
    """WorkOS token verifier that gates on email domain and injects Plane creds.

    Extends the stock userinfo-based verifier (which reliably returns ``email``),
    then rewrites the resulting ``AccessToken`` so the rest of the server treats
    the request as a server-PAT call against a fixed workspace.
    """

    def __init__(
        self,
        *,
        authkit_domain: str,
        workspace_slug: str,
        plane_api_key: str,
        allowed_email_domain: str,
        required_scopes: list[str] | None = None,
    ) -> None:
        super().__init__(authkit_domain=authkit_domain, required_scopes=required_scopes)
        self._workspace_slug = workspace_slug
        self._plane_api_key = plane_api_key
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

        # Inject Plane credentials. client.py uses claims["auth_method"] to pick
        # the api_key path, .token as the api key, and claims["workspace_slug"].
        claims = dict(access.claims)
        claims["auth_method"] = "api_key_env"
        claims["workspace_slug"] = self._workspace_slug
        return AccessToken(
            token=self._plane_api_key,
            client_id=access.client_id,
            scopes=access.scopes,
            expires_at=access.expires_at,
            claims=claims,
        )


def build_workos_provider(*, workspace_slug: str, base_url: str) -> AuthKitProvider:
    """Build an AuthKitProvider bound to one workspace for the OAuth transport.

    Env:
      WORKOS_AUTHKIT_DOMAIN   e.g. https://<proj>.authkit.app  (required)
      WORKOS_CLIENT_ID        client_01...  (recommended: binds JWT audience)
      ALLOWED_EMAIL_DOMAIN    default "slogin.io"
      PLANE_API_KEY           the shared cross-user PAT used to call Plane
    """
    authkit_domain = os.getenv("WORKOS_AUTHKIT_DOMAIN", "")
    if not authkit_domain:
        raise ValueError("WORKOS_AUTHKIT_DOMAIN is not set")

    verifier = SloginWorkOSVerifier(
        authkit_domain=authkit_domain,
        workspace_slug=workspace_slug,
        plane_api_key=os.getenv("PLANE_API_KEY", ""),
        allowed_email_domain=os.getenv("ALLOWED_EMAIL_DOMAIN", "slogin.io"),
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
