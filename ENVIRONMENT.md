# Environment inventory

Every variable the cloud agent stack reads, which deployment unit reads it, and
who provisions it.

Names carried over from HybridInference are kept **unchanged** so that moved
files work without edits (see the move-as-is rule in [AGENTS.md](AGENTS.md)).
Rows marked **NEW** are introduced by the split.

## The one rule

> The gateway's `API_KEY_SECRET` and `JWT_SECRET_KEY` must never appear in this
> repository, in its configuration, or on any host that runs it.

Inference grant tokens are minted *and* verified by the gateway, so the secret
stays there. This service verifies identity tokens with a **public** key from
JWKS, and signs its own tokens with its own secrets. If a task ever seems to
need a gateway secret here, the design is wrong — stop and escalate.

## Provisioning status

| | |
|---|---|
| Staging values provisioned | ☐ not yet — owner: Murphy |
| Production values provisioned | ☐ not yet |
| Secret storage | deploy host environment / systemd unit env files. **Never** in git. |

## Control plane (`backend/cloud_agent`)

### Database

| Variable | New? | Value / who provisions |
|---|---|---|
| `AGENT_DATABASE_URL` | **NEW** | Postgres DSN for the agent-owned database. Decision DR3: same staging instance, new database `cloud_agent`. Murphy provisions. |
| `AGENT_TEST_DATABASE_URL` | **NEW** | Used by `make test-db` only. Local/CI throwaway database. |

### Gateway contracts

| Variable | New? | Value / who provisions |
|---|---|---|
| `GATEWAY_BASE_URL` | **NEW** | `https://staging.freeinference.org` / `https://freeinference.org` |
| `GATEWAY_JWKS_URL` | **NEW** | `${GATEWAY_BASE_URL}/v1/identity/jwks` (task C1) |
| `GATEWAY_GRANT_DISPATCH_TOKEN` | **NEW** | Shared bearer token authorizing this service to mint and revoke inference grants (task C5). Generate random 32 bytes; set the **same** value on the gateway side. Murphy provisions. |
| `AGENT_GATEWAY_URL` | existing | Gateway URL as seen *from inside the sandbox* — the relay address, not the public URL. Differs from `GATEWAY_BASE_URL` by design. |

### Own secrets

| Variable | New? | Value / who provisions |
|---|---|---|
| `AGENT_CONTROL_TOKEN_SECRET` | **NEW** | Signs host/worker control tokens. Replaces the old derivation from the gateway's `API_KEY_SECRET` (task E3). Generate random 32 bytes, unique per environment. |
| `AGENT_SESSION_SECRET` | **NEW** | Signs the web session cookie (task E8). Generate random 32 bytes, unique per environment. |
| `AGENT_DISPATCHER_TOKEN` | existing | Authenticates host agents claiming work. Note: on staging this currently lives in RouteWise's `.env`, not this repo's — verify before cutover. |

### Source control

| Variable | New? | Value / who provisions |
|---|---|---|
| `AGENT_GITHUB_APP_ID` | existing | Decision DR2: reuse org App `4436561`. Callback and setup URLs must be extended to the agent domains before F5. |
| `AGENT_GITHUB_APP_CLIENT_ID` | existing | |
| `AGENT_GITHUB_APP_CLIENT_SECRET` | existing | ⚠️ Rotate during migration — the current value was posted in Slack. |
| `AGENT_GITHUB_APP_PRIVATE_KEY` | existing | PEM inline. Prefer over the `_PATH` form in containers. |
| `AGENT_GITHUB_APP_PRIVATE_KEY_PATH` | existing | Alternative to the above. |
| `AGENT_GITHUB_APP_INSTALL_URL` | existing | |
| `AGENT_GITHUB_API` | existing | Defaults to `https://api.github.com`; override for GHE. |
| `AGENT_GITLAB_OAUTH_CLIENT_ID` | existing | |
| `AGENT_GITLAB_OAUTH_CLIENT_SECRET` | existing | |
| `AGENT_GITLAB_OAUTH_REDIRECT_URI` | existing | Must move to the new agent domain at F5. |
| `AGENT_REPO_ALLOWLIST` | existing | |
| `AGENT_GIT_AUTHOR_NAME` / `AGENT_GIT_AUTHOR_EMAIL` | existing | Commit identity for published work. |

### Scheduling

| Variable | New? | Notes |
|---|---|---|
| `AGENT_LEASE_TTL` | existing | Attempt lease horizon. Grant TTL is derived from this (task E9) — changing one without the other creates either dead grants or grants outliving their attempt. |
| `AGENT_TIMEOUT_S` | existing | |
| `AGENT_RUNNER_HOST` | existing | Single-host fallback. Superseded by the `agent_hosts` table in phase G; kept working for single-host deployments. |

## Host agent (`host/cloud_agent_host`)

| Variable | New? | Notes |
|---|---|---|
| `AGENT_WORKSPACE_BROKER_URL` | existing | Single-host global. Phase G resolves this per host from the database; the env var stays as the fallback when an attempt has no `host_id`. |
| `AGENT_WORKSPACE_BROKER_TOKEN` | existing | |
| `AGENT_WORKER_ID` | existing | |
| `AGENT_WORKDIR_ROOT` | existing | Workspace root. Reused as-is at cutover (H2) so in-flight checkouts survive. |
| `AGENT_SNAPSHOT_ROOT` | existing | |
| `AGENT_SNAPSHOT_TTL_S` / `AGENT_WORKSPACE_TTL_S` | existing | |
| `AGENT_TERMINAL_IDLE_TTL_SECONDS` | existing | |
| `AGENT_TERMINAL_HARD_TTL_SECONDS` | existing | |
| `AGENT_TERMINAL_MAX_SESSIONS` | existing | |
| `AGENT_GENERIC_COMMAND` | existing | |
| `AGENT_ENROLL_TOKEN` | **NEW** | One-time host enrollment token (task G2). Issued from the admin dashboard, consumed once at first boot, then replaced by a stored host credential. |

## Sandbox isolation (host agent reads, sandbox inherits)

| Variable | New? | Notes |
|---|---|---|
| `AGENT_SANDBOX_BACKEND` | existing | |
| `AGENT_SANDBOX_RUNTIME` | existing | `runc` for trusted internal repos; Kata/gVisor required for public multi-tenant use. |
| `AGENT_SANDBOX_IMAGE` | existing | |
| `AGENT_SANDBOX_NETWORK` | existing | |
| `AGENT_SANDBOX_DOCKER` | existing | |
| `AGENT_SANDBOX_HOME` | existing | |
| `AGENT_SANDBOX_UID` / `AGENT_SANDBOX_GID` | existing | Non-root inside the sandbox. |
| `AGENT_SANDBOX_EXTRA_ARGS` | existing | |
| `AGENT_SANDBOX_ALLOW_OPEN_NETWORK` | existing | ⚠️ Isolation escape hatch. Must be unset in any environment reachable by untrusted repositories. |
| `AGENT_SANDBOX_ALLOW_UNISOLATED` | existing | ⚠️ Same. |
| `AGENT_EGRESS_SETUP_TIER` | existing | Egress during dependency setup. |
| `AGENT_EGRESS_AGENT_TIER` | existing | Egress during the agent run. `platform_only` is the safe default; `custom` grants untrusted code general internet access. |
| `AGENT_EGRESS_ALLOWLIST` | existing | |
| `AGENT_EGRESS_NETWORK_PLATFORM_ONLY` | existing | |
| `AGENT_EGRESS_NETWORK_TRUSTED` | existing | |
| `AGENT_EGRESS_NETWORK_CUSTOM` | existing | |
| `AGENT_EGRESS_NETWORK_FULL` | existing | |

## Web (`web/`)

| Variable | New? | Notes |
|---|---|---|
| `NEXT_PUBLIC_AGENT_API_URL` | **NEW** | Control plane base URL. |
| `NEXT_PUBLIC_GATEWAY_AUTHORIZE_URL` | **NEW** | `${GATEWAY_BASE_URL}/authorize` (task C4). |

## Set on the HybridInference side — not here

These are provisioned in the gateway's environment. Listed so the contract has
one place to be read from end to end.

| Variable | Task | Notes |
|---|---|---|
| `IDENTITY_JWT_PRIVATE_KEY` | C1 | RS256 private key, PEM. Public half is served at `/v1/identity/jwks`. |
| `IDENTITY_ALLOWED_REDIRECTS` | C2 | Comma-separated exact-match redirect URIs. Must include the agent web callback before F5. |
| `GATEWAY_GRANT_DISPATCH_TOKEN` | C5 | Same value as the control plane's copy above. |

## Infrastructure (not environment variables)

- **Domains** (DR4): `agents.staging.freeinference.org`, later
  `agents.freeinference.org`. API served from the same host under `/api`.
- **Staging topology**: staging runs on `spark2` behind a bore tunnel exposed at
  `freeinference.org:10025`. The agent stack needs its own tunnel port
  allocated — assign and record it here before F5.
- **Sandbox image registry**: where `AGENT_SANDBOX_IMAGE` is pulled from. Decide
  before D7.

## Adding a variable

1. Add a row here, in the right section, marked **NEW**.
2. Note who provisions it and whether it differs per environment.
3. If it is a secret, say where it is stored — never the value itself.
4. The PR checklist enforces this; a new variable without a row is a review stop.
