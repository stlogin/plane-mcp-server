"""`/link` — self-service page where a user binds their own Plane PAT to their
Google identity, for the WorkOS OAuth path (claude.ai / mobile).

Flow: Google sign-in (reusing the existing Google OAuth client; @slogin.io only)
→ paste your Plane API token → we validate it against Plane and store it encrypted
keyed by your email. The OAuth verifier then calls Plane *as you*.

Served by the MCP server (Starlette routes), reached via Caddy `/link*`.
Sessions and the OAuth `state` are signed with the same Fernet key as the token
store (`MCP_PAT_ENC_KEY`); no extra dependency.
"""

from __future__ import annotations

import base64
import html
import json
import os
import secrets
import urllib.parse

import httpx
from cryptography.fernet import Fernet
from fastmcp.utilities.logging import get_logger
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response
from starlette.routing import Route

from plane_mcp.user_pat_store import get_user_pat_store

logger = get_logger(__name__)

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"

SESSION_COOKIE = "mcp_link_session"
OAUTH_STATE_COOKIE = "mcp_link_oauth_state"
SESSION_TTL = 1800  # 30 min
STATE_TTL = 600  # 10 min


def _fernet() -> Fernet:
    key = os.getenv("MCP_PAT_ENC_KEY", "")
    if not key:
        raise ValueError("MCP_PAT_ENC_KEY is not set")
    return Fernet(key.encode() if isinstance(key, str) else key)


def _allowed_domain() -> str:
    return (os.getenv("ALLOWED_EMAIL_DOMAIN", "slogin.io") or "").strip().lower().lstrip("@")


def _public_base() -> str:
    return (os.getenv("PUBLIC_BASE_URL", "") or "").rstrip("/")


def _redirect_uri() -> str:
    # Trailing slash matches the Plane Google client's other redirect URIs.
    return f"{_public_base()}/link/callback/"


def _sign(purpose: str, data: dict) -> str:
    payload = {"p": purpose, **data}
    return _fernet().encrypt(json.dumps(payload).encode()).decode()


def _unsign(purpose: str, token: str, ttl: int) -> dict | None:
    try:
        raw = _fernet().decrypt(token.encode(), ttl=ttl)
    except Exception:
        return None
    try:
        data = json.loads(raw)
    except Exception:
        return None
    if data.get("p") != purpose:
        return None
    return data


def _session_email(request: Request) -> str | None:
    cookie = request.cookies.get(SESSION_COOKIE)
    if not cookie:
        return None
    data = _unsign("session", cookie, SESSION_TTL)
    return data.get("email") if data else None


def _decode_id_token_email(id_token: str) -> tuple[str | None, bool]:
    """Decode the (already-trusted, fetched over TLS from Google) id_token payload."""
    try:
        payload_b64 = id_token.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
    except Exception:
        return None, False
    return payload.get("email"), bool(payload.get("email_verified"))


# --- HTML -----------------------------------------------------------------

_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Plane MCP — link your token</title>
<style>
body{{margin:0;background:#0f1115;color:#e7ebf3;font:15px/1.6 -apple-system,Segoe UI,Roboto,sans-serif}}
.card{{max-width:520px;margin:8vh auto;background:#171a21;border:1px solid #2a2f3a;border-radius:14px;padding:28px}}
h1{{font-size:20px;margin:0 0 4px}} p{{color:#9aa3b2;font-size:14px}}
.btn{{display:inline-block;border:0;border-radius:9px;padding:11px 16px;font-size:14px;font-weight:600;cursor:pointer}}
.g{{background:#fff;color:#1f1f1f}} .primary{{background:#28c08a;color:#06281f}}
.danger{{background:#3a2226;color:#e3859a;border:1px solid #5a2f38}}
input{{width:100%;background:#0b0d12;border:1px solid #2a2f3a;border-radius:9px;
padding:11px;color:#e7ebf3;font-family:ui-monospace,Menlo,monospace;font-size:13px;box-sizing:border-box}}
label{{display:block;font-size:12px;color:#9aa3b2;margin:14px 0 6px}}
.ok{{color:#28c08a}} .err{{color:#e3859a}}
code{{background:#0b0d12;border:1px solid #2a2f3a;border-radius:5px;padding:1px 6px;font-size:12px}}
.muted{{color:#9aa3b2;font-size:12.5px;margin-top:14px}}
</style></head><body><div class="card">{body}</div></body></html>"""


def _html(body: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(_PAGE.format(body=body), status_code=status)


# --- routes ---------------------------------------------------------------

async def link_index(request: Request) -> Response:
    email = _session_email(request)
    if not email:
        return _html(
            "<h1>Plane MCP — link your token</h1>"
            "<p>Connect your personal Plane API token so claude.ai / the mobile app "
            "act as <b>you</b> (you only see your own workspaces).</p>"
            '<p><a class="btn g" href="/link/login">Sign in with Google</a></p>'
            f"<p class='muted'>Use your <code>@{_allowed_domain()}</code> Google account.</p>"
        )

    store = get_user_pat_store()
    registered = await store.has_pat(email)
    csrf = _sign("csrf", {"email": email})
    status_line = (
        '<p class="ok">✓ A token is currently registered for this account.</p>'
        if registered
        else '<p class="muted">No token registered yet.</p>'
    )
    delete_btn = (
        f'<form method="post" action="/link/delete" style="margin-top:18px">'
        f'<input type="hidden" name="csrf" value="{csrf}">'
        f'<button class="btn danger" type="submit">Remove my token</button></form>'
        if registered
        else ""
    )
    return _html(
        f"<h1>Hi {html.escape(email)}</h1>"
        "<p>Paste your Plane API token. Get it in Plane: avatar → "
        "<b>Settings → API tokens → Add API token</b>.</p>"
        '<form method="post" action="/link/save">'
        f'<input type="hidden" name="csrf" value="{csrf}">'
        '<label>Plane API token</label>'
        '<input name="pat" placeholder="plane_api_…" autocomplete="off" spellcheck="false" required>'
        '<p style="margin-top:16px"><button class="btn primary" type="submit">Save &amp; link</button></p>'
        "</form>" + status_line + delete_btn +
        "<p class='muted'>Stored encrypted, keyed to your email. We validate it against Plane on save.</p>"
    )


async def link_login(request: Request) -> Response:
    client_id = os.getenv("GOOGLE_OAUTH_CLIENT_ID", "")
    if not client_id:
        return _html('<h1>Misconfigured</h1><p class="err">GOOGLE_OAUTH_CLIENT_ID not set.</p>', 500)
    # Bind the OAuth flow to this browser: the nonce goes both into the signed
    # `state` (sent to Google) and into an HttpOnly cookie. The callback requires
    # both to match, preventing login CSRF / credential-takeover via a planted code.
    nonce = secrets.token_urlsafe(16)
    state = _sign("state", {"n": nonce})
    params = {
        "client_id": client_id,
        "redirect_uri": _redirect_uri(),
        "response_type": "code",
        "scope": "openid email",
        "state": state,
        "prompt": "select_account",
        "hd": _allowed_domain(),
    }
    redirect = RedirectResponse(f"{GOOGLE_AUTH_URL}?{urllib.parse.urlencode(params)}", status_code=302)
    redirect.set_cookie(
        OAUTH_STATE_COOKIE, nonce, max_age=STATE_TTL,
        httponly=True, secure=True, samesite="lax", path="/link",
    )
    return redirect


async def link_callback(request: Request) -> Response:
    if request.query_params.get("error"):
        err = html.escape(request.query_params.get("error", ""))
        return _html(f'<h1>Sign-in failed</h1><p class="err">{err}</p>', 400)
    code = request.query_params.get("code")
    state_data = _unsign("state", request.query_params.get("state", ""), STATE_TTL)
    cookie_nonce = request.cookies.get(OAUTH_STATE_COOKIE)
    # state must be valid AND its nonce must match the cookie set at /link/login
    # (binds the flow to this browser → defeats login CSRF).
    if not code or not state_data or not cookie_nonce or state_data.get("n") != cookie_nonce:
        return _html('<h1>Sign-in failed</h1><p class="err">Invalid or expired state.</p>', 400)

    data = {
        "code": code,
        "client_id": os.getenv("GOOGLE_OAUTH_CLIENT_ID", ""),
        "client_secret": os.getenv("GOOGLE_OAUTH_CLIENT_SECRET", ""),
        "redirect_uri": _redirect_uri(),
        "grant_type": "authorization_code",
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(GOOGLE_TOKEN_URL, data=data)
    except httpx.RequestError as exc:
        return _html(f'<h1>Sign-in failed</h1><p class="err">Token exchange error: {html.escape(str(exc))}</p>', 502)
    if resp.status_code != 200:
        return _html('<h1>Sign-in failed</h1><p class="err">Token exchange rejected.</p>', 400)

    try:
        id_token = resp.json().get("id_token", "")
    except ValueError:
        id_token = ""
    email, verified = _decode_id_token_email(id_token)
    email = (email or "").strip().lower()
    domain = _allowed_domain()
    if not email or not verified or (domain and not email.endswith(f"@{domain}")):
        logger.warning("link callback rejected: email=%r verified=%s", email, verified)
        return _html(f'<h1>Not allowed</h1><p class="err">A verified @{domain} Google account is required.</p>', 403)

    redirect = RedirectResponse("/link", status_code=302)
    redirect.set_cookie(
        SESSION_COOKIE,
        _sign("session", {"email": email}),
        max_age=SESSION_TTL,
        httponly=True,
        secure=True,
        samesite="lax",
        path="/link",
    )
    redirect.delete_cookie(OAUTH_STATE_COOKIE, path="/link")
    return redirect


async def link_save(request: Request) -> Response:
    email = _session_email(request)
    if not email:
        return RedirectResponse("/link", status_code=302)
    form = await request.form()
    csrf = _unsign("csrf", str(form.get("csrf", "")), SESSION_TTL)
    if not csrf or csrf.get("email") != email:
        return _html('<h1>Error</h1><p class="err">Invalid form token. Reload and retry.</p>', 400)
    pat = str(form.get("pat", "")).strip()
    if not pat:
        return _html('<h1>Error</h1><p class="err">Token is empty.</p>', 400)

    # Validate the PAT against Plane (X-API-Key is how Plane authenticates PATs).
    base = (os.getenv("PLANE_INTERNAL_BASE_URL") or os.getenv("PLANE_BASE_URL", "")).rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            me = await client.get(f"{base}/api/v1/users/me/", headers={"X-API-Key": pat})
    except httpx.RequestError as exc:
        return _html(f'<h1>Could not validate</h1><p class="err">Plane request error: {html.escape(str(exc))}</p>', 502)
    if me.status_code != 200:
        return _html(
            '<h1>Invalid token</h1><p class="err">Plane rejected this token '
            f'(HTTP {me.status_code}). Check you copied it correctly.</p>'
            '<p><a class="btn g" href="/link">Back</a></p>',
            400,
        )

    await get_user_pat_store().set_pat(email, pat)
    logger.info("Linked Plane PAT for %s", email)
    return _html(
        "<h1 class='ok'>✓ Linked</h1>"
        f"<p>Your Plane token is now linked to <b>{html.escape(email)}</b>. "
        "Connect the MCP connector in claude.ai and sign in with Google — "
        "you'll see only your own workspaces.</p>"
        '<p><a class="btn g" href="/link">Back</a></p>'
    )


async def link_delete(request: Request) -> Response:
    email = _session_email(request)
    if not email:
        return RedirectResponse("/link", status_code=302)
    form = await request.form()
    csrf = _unsign("csrf", str(form.get("csrf", "")), SESSION_TTL)
    if not csrf or csrf.get("email") != email:
        return _html('<h1>Error</h1><p class="err">Invalid form token. Reload and retry.</p>', 400)
    await get_user_pat_store().delete_pat(email)
    logger.info("Unlinked Plane PAT for %s", email)
    return _html("<h1>Removed</h1><p>Your token registration was deleted.</p>"
                 '<p><a class="btn g" href="/link">Back</a></p>')


def get_link_routes() -> list[Route]:
    """Routes for the /link self-service registration page."""
    return [
        Route("/link", link_index, methods=["GET"]),
        Route("/link/login", link_login, methods=["GET"]),
        Route("/link/callback/", link_callback, methods=["GET"]),
        Route("/link/save", link_save, methods=["POST"]),
        Route("/link/delete", link_delete, methods=["POST"]),
    ]
