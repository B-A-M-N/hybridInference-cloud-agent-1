# Environment inventory

Every variable the cloud agent stack reads, which deployment unit reads it, and
who provisions it.

Names carried over from HybridInference are kept **unchanged** so that moved
files work without edits (see the move-as-is rule in [AGENTS.md](AGENTS.md)).
Rows marked **NEW** are introduced by the split.

> **This file is public. It names variables, never values.**
>
> No identifiers, hostnames, ports, tunnel topology, or app registrations belong
> here — not because they are secret in the cryptographic sense, but because a
> published inventory of a deployment's internals is a gift to anyone probing it,
> and this repository ships to everyone who clones it. Real values live in the
> operator's private deployment overlay.
>
> A row that needs a concrete example uses an obvious placeholder
> (`agents.example.org`). If you find yourself pasting something from a running
> deployment, it goes in the overlay instead.

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
| Staging values provisioned | ☐ not yet |
| Production values provisioned | ☐ not yet |
| Secret storage | deploy host environment / unit env files, and read-only file mounts for keys. **Never** in git. |

## Control plane (`backend/cloud_agent`)

### Database

| Variable | New? | Value / who provisions |
|---|---|---|
| `AGENT_DATABASE_URL` | **NEW** | Postgres DSN for the agent-owned database. Must be a database this service owns outright — sharing one with the gateway would reintroduce the coupling the split exists to remove. |
| `AGENT_TEST_DATABASE_URL` | **NEW** | Used by `make test-db` only. Local/CI throwaway database. |

### Gateway contracts

| Variable | New? | Value / who provisions |
|---|---|---|
| `GATEWAY_BASE_URL` | **NEW** | Origin of the FreeInference gateway this deployment federates with, e.g. `https://gateway.example.org`. |
| `GATEWAY_JWKS_URL` | **NEW** | `${GATEWAY_BASE_URL}/v1/identity/jwks` (task C1). Publishes the current signing key plus any retiring ones, so cache by `kid` and re-fetch on a miss rather than pinning a single key. |
| `GATEWAY_GRANT_DISPATCH_TOKEN` | **NEW** | Bearer token authorizing this service to mint and revoke inference grants (task C5). Random 32 bytes, the same value on both sides, and distinct per environment — one shared across staging and production makes a staging compromise a production one. |
| `AGENT_GATEWAY_URL` | existing | Gateway URL as seen *from inside the sandbox* — the relay address, not the public URL. Differs from `GATEWAY_BASE_URL` by design. |

### Own secrets

| Variable | New? | Value / who provisions |
|---|---|---|
| `AGENT_SOURCE_CONTROL_ENCRYPTION_KEY` | **NEW** | Wraps stored GitLab/GitHub credentials. Replaces the gateway-derived key those secrets were encrypted with before the split, which is why migrating them needs a re-wrap step and not a copy (task H1). Generate random 32 bytes, unique per environment. |
| `AGENT_CONTROL_TOKEN_SECRET` | **NEW** | Signs host/worker control tokens. Replaces the old derivation from the gateway's `API_KEY_SECRET` (task E3). Generate random 32 bytes, unique per environment. |
| `AGENT_SESSION_SECRET` | **NEW** | Signs the web session cookie (task E8). Generate random 32 bytes, unique per environment. |
| `AGENT_DISPATCHER_TOKEN` | existing | Authenticates host agents claiming work. Existing deployments may hold this outside this service's own configuration; confirm where it is actually read from before cutover rather than assuming. |

### Source control

| Variable | New? | Value / who provisions |
|---|---|---|
| `AGENT_GITHUB_APP_ID` | existing | The GitHub App backing repository access. Whether to reuse an existing App or register a new one is decision DR2, recorded in the operator's overlay. Either way its callback and setup URLs must list the agent domains before F5. |
| `AGENT_GITHUB_APP_CLIENT_ID` | existing | |
| `AGENT_GITHUB_APP_CLIENT_SECRET` | existing | Rotate when the App's audience changes — moving to a new domain is a good moment to retire whatever the old value was exposed to. |
| `AGENT_GITHUB_APP_PRIVATE_KEY_PATH` | existing | **Preferred.** Path to a read-only mounted key file. A private key in an environment variable is readable by anything that can list the process environment, and lands in `docker inspect`, crash dumps, and most log shippers. |
| `AGENT_GITHUB_APP_PRIVATE_KEY` | existing | Inline PEM. Supported, but use the `_PATH` form where a file mount is possible. |
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
| `IDENTITY_ISSUER` | C3 | The `iss` claim in identity tokens. Falls back to the gateway's `base_url`; unset means issuance is switched off rather than tokens minted with an empty `iss`. Must equal the value this service verifies against. |
| `IDENTITY_JWT_RETIRING_PUBLIC_KEYS` | C1 | Public PEMs of previously-active signing keys, concatenated. Published alongside the current key so tokens issued before a rotation still verify. Keep a key here for at least the maximum token lifetime plus the JWKS cache window, then drop it. Public halves only — a retiring key must not be able to sign. |
| `IDENTITY_ALLOWED_REDIRECTS` | C2 | Comma-separated exact-match redirect URIs. Matching is equality — no prefix, no suffix — so the agent web callback must be listed verbatim before F5. |
| `GATEWAY_GRANT_DISPATCH_TOKEN` | C5 | Same value as the control plane's copy above. |

## Infrastructure (not environment variables)

Recorded here as *what has to be decided*, not as anyone's actual answer.

- **Domains** (DR4): one host for the web app and the control-plane API, the API
  under `/api` — e.g. `agents.example.org` and a staging sibling. The exact names
  go in the overlay, and must match `IDENTITY_ALLOWED_REDIRECTS` on the gateway
  exactly.
- **Reaching the gateway**: the control plane needs a route to `GATEWAY_BASE_URL`,
  and the sandbox needs one to the relay and nothing else. If a deployment
  fronts either through a tunnel, the port allocation is overlay detail.
- **Sandbox image registry**: where `AGENT_SANDBOX_IMAGE` is pulled from. Decide
  before D7.

## Adding a variable

1. Add a row here, in the right section, marked **NEW**.
2. Note who provisions it and whether it differs per environment.
3. If it is a secret, say where it is stored — never the value itself.
4. The PR checklist enforces this; a new variable without a row is a review stop.
