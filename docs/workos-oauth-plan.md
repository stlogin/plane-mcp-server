# WorkOS OAuth for plane-mcp-server — implementation plan

**Status:** implemented · PoC passed (claude.ai ↔ Google ↔ tools) · deploying to prod · 2026-06-19

**PoC learning:** the WorkOS `/oauth2/userinfo` returned an empty `email` until we
advertised `scopes_supported=["openid","email","profile"]` on the `AuthKitProvider`
(without the `email` scope the client never requests it, so userinfo omits it and the
server-side `@slogin.io` check can't pass). With the scope advertised, the domain check
works from userinfo alone — no WorkOS Management API lookup needed.

**As built:** new `workos` server mode in `__main__.py` serves the header/PAT endpoint
(`/mcp`, Claude Code) plus one WorkOS-OAuth endpoint per workspace
(`/mcp-oauth/<slug>/mcp`). Deployed via the `plane-mcp` container `command: ["workos"]`
with WorkOS env in `plane.env`. WorkOS stays on the staging AuthKit project for now;
hardening (own Google OAuth client, Google-only, production WorkOS env) is the follow-up.

## Goal

Let **claude.ai / the Claude mobile app** connect to our self-hosted Plane MCP over
**OAuth** (sign in with **Google, slogin.io only**), while **keeping the existing
header/PAT path** for Claude Code on the desktop.

Why OAuth is required: claude.ai custom connectors only accept **OAuth** (the
connector UI has no Bearer/custom-header field), and they need OAuth 2.1 with
**DCR/CIMD + PKCE + protected-resource-metadata**. Our current `/http/api-key`
(Bearer + `x-workspace-slug`) works in Claude Code but cannot be used from
claude.ai/mobile.

## Current state (the fork)

- Built on **FastMCP** (`fastmcp==3.2.0`, Python/uv).
- `plane_mcp/server.py` has three factories: `get_oauth_mcp` (auth=`PlaneOAuthProvider`,
  an `OAuthProxy` to **Plane's** OAuth — Cloud model), `get_header_mcp`
  (`PlaneHeaderAuthProvider`: `x-api-key` + `x-workspace-slug`), `get_stdio_mcp`.
- `plane_mcp/__main__.py` `http` mode serves **both** endpoints simultaneously via
  Starlette `Mount`: OAuth at `…/http`, header at `…/http/api-key`, sse at `/`.
- **All auth funnels through claims**: `client.py::get_plane_client_context()` reads
  `get_access_token().claims` for `auth_method`, `token`, `workspace_slug`, and builds
  the `PlaneClient` accordingly (`api_key_*` → use as api_key; else → access_token).
  - `PlaneHeaderAuthProvider` → claims `{auth_method:"api_key_header", token:<api key>, workspace_slug:<header>}`.
  - `PlaneOAuthProvider` → claims `{auth_method:"oauth", token:<plane oauth token>, workspace_slug:<from Plane app installation>}`.
- On EC2 the container runs behind Caddy; `.mcp.json` currently uses the header path.

**Key consequence:** the OAuth token today **is a Plane token** and the workspace
comes from the **Plane** OAuth app installation. WorkOS gives a **Google-identity JWT**
— no Plane token, no workspace. That gap is the core of this work.

## Target architecture

```
Phone / claude.ai ─ OAuth (WorkOS AuthKit / Google@slogin.io) ─▶ /mcp-oauth/<workspace>
Claude Code (PC)  ─ Bearer + x-workspace-slug ───────────────▶ /http/api-key/mcp   (unchanged)
                                                                  │
                                                                  ▼  same tools (one fork)
                                                       plane-mcp-server (FastMCP)
                                                         OAuth path → AuthKitProvider verifies WorkOS JWT
                                                                      → inject claims (server PAT + workspace)
                                                         PAT path   → header token (as today)
                                                                  ▼
                                                          Plane REST API (/api/v1)
```

## Design decisions

- **Provider:** FastMCP **`AuthKitProvider`** (WorkOS **AuthKit**, DCR/CIMD) from
  `fastmcp.server.auth.providers.workos` — **NOT** `WorkOSProvider` (that's WorkOS
  *Connect*). The official ref is the FastMCP **"AuthKit 🤝 FastMCP"** page.
- **Replace, don't stack:** swap the OAuth endpoint's `PlaneOAuthProvider` for the
  WorkOS provider (we don't use Plane-OAuth; CE may not even support it). The
  **header/PAT endpoint stays untouched** (= "keep the API method").
- **WorkOS JWT → Plane credentials:** after JWT verification, inject claims so
  `client.py` works **unchanged**: `auth_method="api_key_env"`, `token=<server-side
  shared PAT>`, `workspace_slug=<from the route>`. (Preferred: do all injection in the
  provider/middleware so `client.py` needs no edit.)
- **Workspace without headers:** claude.ai can't send `x-workspace-slug`, so workspace
  comes from the **URL path** — one OAuth mount per workspace (`/mcp-oauth/sli-dev`, …),
  matching the existing Starlette `Mount` pattern. Each maps to its own Resource Indicator.
- **slogin.io restriction:** WorkOS can be set to **Google-only**, but **does not
  auto-restrict the domain** for Google OAuth → enforce **server-side**: reject any
  JWT whose `email` is not `@slogin.io` (or use a WorkOS org enforced-domain policy).
- **Identity/attribution tradeoff:** the OAuth path calls Plane with the **shared PAT**,
  so Plane records the **PAT owner** as the actor (same as the header path today). The
  Google user is still captured in MCP logs (`UserContextFilter` → `sub`). No per-user
  Plane attribution. Authorization granularity = "authenticated + slogin.io" only.

## Files to change

| File | Change |
|---|---|
| `plane_mcp/auth/workos_auth_provider.py` *(new)* | Wrap/subclass `AuthKitProvider` (or its `TokenVerifier`): verify WorkOS JWT, **enforce `@slogin.io` email**, **inject claims** (`auth_method="api_key_env"`, `token`=server PAT, `workspace_slug`). |
| `plane_mcp/auth/__init__.py` | export the new provider |
| `plane_mcp/server.py` | add `get_workos_mcp(workspace_slug)` factory using the WorkOS provider; keep `get_header_mcp`/`get_stdio_mcp`. Decide whether `get_oauth_mcp` is replaced or kept. |
| `plane_mcp/__main__.py` | add per-workspace mounts (`/mcp-oauth/<slug>`) + well-known routes per resource; new env wiring. |
| `plane_mcp/client.py` | ideally **no change** (claims injection covers it); confirm `api_key_env` path uses the injected token+workspace. |
| `tests/test_oauth_security.py` | add cases: valid slogin.io JWT → ok; non-slogin.io → 403; workspace resolved from route. |
| env / deploy (`plane.slogin.io` repo) | `docker-compose.*` env (`WORKOS_AUTHKIT_DOMAIN`, server PAT, `ALLOWED_EMAIL_DOMAIN=slogin.io`, base_url); Caddy route(s) for `/mcp-oauth/*`; rebuild+push `ghcr.io/stlogin/plane-mcp-server`. |

## WorkOS dashboard checklist

- [ ] Create the AuthKit project → note `authkit_domain` (`https://<proj>.authkit.app`).
- [ ] Connect → Configuration: enable **CIMD** (and **DCR** for older clients).
- [ ] Add each MCP OAuth endpoint URL as a **Resource Indicator** (one per workspace).
- [ ] Authentication methods: **enable Google only** (disable password / magic link / others).
- [ ] Domain restriction: either a WorkOS **org enforced-domain** policy *or* rely on the
      **server-side email check** (plan above). Pick one and record it.
- [ ] Confirm claude.ai callback works (FastMCP already whitelists `https://claude.ai/*`).

## PoC (do this before full build)

1. One workspace (`sli-dev`), one WorkOS AuthKit project, run a single WorkOS-OAuth
   endpoint (locally or on EC2).
2. Add it as a **custom connector on claude.ai (web)** → sign in with Google.
3. Verify: `tools/list` returns, a **read tool works**, a **non-slogin.io account is
   rejected**, and the workspace is correctly bound from the route.
4. Only then: generalize to per-workspace mounts, wire deploy, add tests.

## Risks / open questions

- **FastMCP AuthKitProvider real-world bugs**: e.g. issue #1392 (inspector). API exists,
  but "works with claude.ai specifically" must be proven in the PoC.
- **Claim-injection hook**: confirm `AuthKitProvider` exposes a clean way to add claims /
  enforce domain post-verify (likely subclass its `TokenVerifier`). Main technical unknown.
- **Resource Indicator per workspace**: each `/mcp-oauth/<slug>` needs its own registered
  resource URL in WorkOS so the `aud` matches.
- **fastmcp pinned at 3.2.0** (AuthKitProvider available since 2.11; bump to latest is a
  separate, optional chore).

## Out of scope

- Per-user Plane attribution / per-user Plane tokens.
- Per-tool RBAC beyond "authenticated + slogin.io".
- Changing the header/PAT path (kept as-is).

## References

- FastMCP — [AuthKit 🤝 FastMCP (`AuthKitProvider`, DCR)](https://gofastmcp.com/integrations/authkit) · [PR #1327 (WorkOS OAuth 2.1)](https://github.com/PrefectHQ/fastmcp/pull/1327) · [issue #1392](https://github.com/jlowin/fastmcp/issues/1392)
- WorkOS — [AuthKit for MCP](https://workos.com/docs/authkit/mcp) · [Google OAuth integration](https://workos.com/docs/integrations/google-oauth)
- Code — `plane_mcp/{server,client,__main__}.py`, `plane_mcp/auth/plane_oauth_provider.py`
- Higher-level context — `plane.slogin.io/docs/mobile-mcp-connector-plan.md`
