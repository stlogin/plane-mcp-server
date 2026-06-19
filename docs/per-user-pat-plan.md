# Per-user Plane PAT for the WorkOS OAuth path (Option A)

**Status:** planning → implementing · 2026-06-19

## Goal

Make the **claude.ai / mobile (WorkOS OAuth)** path act as the **individual user**,
so each person sees only the Plane workspaces they belong to. We map the WorkOS
identity (email) → that user's **own Plane PAT**, and call Plane with it. Plane's
existing RBAC then enforces membership for free.

This only affects the OAuth path. **Claude Code (header/PAT) is unchanged** — there a
user already puts their own PAT in `.mcp.json`, so per-account already works and `/link`
is not needed.

Plane CE has no OAuth-app support (verified: `/auth/o/*` → 404), so the cleaner
"Plane OAuth on-behalf-of" (Option B) is impossible here; A is the realistic path.

## How it works

```
claude.ai → WorkOS (Google, slogin.io) → MCP verify_token gets email
         → look up email → user's encrypted PAT in the store
         → inject claims {auth_method: api_key_env, token: <user PAT>, workspace_slug}
         → client.py calls Plane with the user's PAT → Plane RBAC filters by membership
```

One-time per user: create a PAT in Plane → register it once at `/link` (Google sign-in
binds it to their email). Daily use: just sign in with Google; the connector never asks
for a token.

## Components

### 1. Store — `plane_mcp/user_pat_store.py`
- **SQLite** at `MCP_PAT_DB_PATH` (default `/data/user_pat.db`, a mounted volume so it's
  in the daily EBS snapshot). Table: `user_pat(email TEXT PRIMARY KEY, pat_encrypted BLOB,
  updated_at TEXT)`.
- PAT values encrypted at rest with **Fernet** (`cryptography`), key `MCP_PAT_ENC_KEY`.
- Async API (`get_pat`/`set_pat`/`delete_pat`) wrapping blocking sqlite in `asyncio.to_thread`.
- Email normalised to lowercase.

### 2. Registration page — `plane_mcp/link_app.py` (Starlette routes, served by the MCP)
- `GET /link` — if no session: "Sign in with Google"; if session: show PAT form +
  current status (registered / not), with delete.
- `GET /link/login` — redirect to Google OAuth authorize (`openid email`, state=Fernet-signed).
- `GET /link/callback` — verify state, exchange code (Google token endpoint, httpx),
  read email + `email_verified`, enforce `@slogin.io`, set a short-lived Fernet-signed
  session cookie, redirect to `/link`.
- `POST /link/save` — CSRF check; read email from session; validate the submitted PAT via
  `GET {PLANE_INTERNAL_BASE_URL}/api/v1/users/me/` (Bearer); on 200, encrypt + store.
- `POST /link/delete` — remove the registration.
- Session: Fernet-signed cookie `mcp_link_session` = `{email, exp}` (~30 min). CSRF token
  derived from the session, checked on POST.
- Reuses the **existing Google OAuth client** (`google-oauth.env`, shared with Plane login)
  — just add `{PUBLIC_BASE_URL}/link/callback` to its authorized redirect URIs.

### 3. Verifier change — `plane_mcp/auth/workos_auth_provider.py`
- After the existing `email_verified` + `@slogin.io` checks, look up
  `pat = await store.get_pat(email)`.
- If found → inject `auth_method="api_key_env"`, `token=pat` (instead of the shared env PAT).
- If not found → reject (401) and log `no PAT registered for <email> — see /link`.
- Transition switch `PER_USER_PAT_REQUIRED` (default `true`). When `false`, fall back to the
  shared `PLANE_API_KEY` if the user hasn't registered (eases rollout).

### 4. Config / deploy
- New env (host `plane.env`): `MCP_PAT_ENC_KEY` (Fernet key), `MCP_PAT_DB_PATH=/data/user_pat.db`,
  `GOOGLE_OAUTH_CLIENT_ID` + `GOOGLE_OAUTH_CLIENT_SECRET` (reuse existing values),
  `PER_USER_PAT_REQUIRED`. (`PUBLIC_BASE_URL` already set.)
- `docker-compose.patch.yaml`: mount a named volume at `/data` for the SQLite file.
- `Caddyfile`: route `/link*` → `plane-mcp:8211` (same pattern as the `.well-known` routes).
- GCP: add `{PUBLIC_BASE_URL}/link/callback` to the existing Google OAuth client's redirect URIs.

## Security
- PATs encrypted at rest (Fernet); key only in `plane.env`; never logged.
- `/link` login is Google + `email_verified` + `@slogin.io`; session cookie signed + short TTL;
  CSRF on POST.
- SQLite on a single-replica container (replicas=1) → single writer, no contention.
- Lifecycle: re-`/link` after rotating a PAT; delete on offboarding; unregistered → rejected.

## Out of scope (Phase 2)
- URL unification (one connector + `list_my_workspaces` + per-call `workspace_slug`).
- Admin UI to list/revoke registrations (CLI/SQL for now).

## Test plan
- Store: set/get/delete round-trip with encryption.
- `/link`: state/CSRF, domain rejection, PAT validation against `/users/me/`.
- Verifier: registered email → injects user PAT; unregistered → 401; fallback flag behaviour.
