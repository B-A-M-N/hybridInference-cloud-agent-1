# AGENTS.md

Onboarding guide for anyone working in this repository — human or AI coding
agent. Read this first.

## 1. What this is

FreeInference Cloud Agent runs coding agents (Claude Code, Codex, Kilo, …) in
sandboxed containers against a user's repository and publishes results as pull
requests. It is being **extracted from the HybridInference monorepo**
(`HarvardMadSys/hybridInference`) into this repository as an independent service.

The extraction plan is the source of truth for what to build and in what order:
`docs/agents/plans/2026-08-03-cloud-agent-repo-split.md` in the HybridInference
repo, with the file-level manifest in `cloud-agent-split-manifest.md` beside it.
[MIGRATION.md](MIGRATION.md) records what has actually landed here.

## 2. Repo map

```text
backend/cloud_agent/    control plane (FastAPI): API, auth adapter, scheduler,
                        source-control integrations, publishing, storage
host/cloud_agent_host/  host agent: runner, sandbox, runtimes, workspace broker
web/                    Next.js user UI + admin dashboard
contracts/              OpenAPI mirrors of the gateway contracts we consume
migrations/             Alembic migrations (agent-owned database only)
deploy/{control-plane,host,sandbox}/
tests/{unit,servers,integration}/
docs/
```

## 3. Quality gates

```bash
make setup-dev   # uv venv + uv sync
make check       # format + lint + test — must be green before every PR
```

`make test` runs everything except the `external` and `dbtest` markers.
Database-backed tests are opt-in: `make test-db` (needs Postgres; see
[ENVIRONMENT.md](ENVIRONMENT.md)).

## 4. Workflow

- Branch off `dev` as `<user>/<scope>/<name>`. Pull requests target `dev`.
- One task from the extraction plan = one pull request.
- Never commit secrets. This repository will be made public; treat every commit
  as already public.

## 5. Migration rules — in force until the split completes

These override normal engineering instincts. They exist because a migration
whose diff mixes moves with edits cannot be reviewed, and because a parity claim
needs an unchanged baseline to be worth anything.

1. **Move as-is.** When a task says "move" a file, change imports and paths
   only. No renames, no splitting large files, no reformatting, no dead-code
   removal, no "while I'm here" fixes. A reviewer will diff your result against
   a pristine copy from the source repo and expect only import lines to differ.
   Cleanups happen after cutover, as their own pull requests.
2. **If it cannot move without a logic change, stop and report.** Do not
   improvise a redesign inside a migration task.
3. **Never weaken a test to make it pass.** A ported test that fails is a parity
   break — report it. Adapting fixtures and imports is fine; changing an
   assertion is not.
4. **No gateway secrets here.** This service never sees the FreeInference
   gateway's `API_KEY_SECRET` or `JWT_SECRET_KEY`. Grant tokens are minted *and*
   verified by the gateway. Our own tokens use our own secrets
   (`AGENT_CONTROL_TOKEN_SECRET`, `AGENT_SESSION_SECRET`).
5. **The gateway is reached over HTTP only.** No Python import of `serving.*`,
   ever. If you need something the gateway knows, it needs a contract in
   `contracts/`.

## 6. Key concepts

- **Job / attempt / lease.** A job is the user's task; an attempt is one
  execution of it on one host. Attempts carry a lease with a fencing
  generation — a superseded attempt's writes are rejected by the store, which
  is what makes retries safe and stops duplicate pull requests.
- **Grant.** A short-lived, per-attempt capability token minted by the gateway,
  scoped to a user, job, model allowlist, and dollar budget. It is the only
  credential that enters the sandbox. Revoked when the attempt is superseded.
- **Host.** A machine in the pool running the host agent. It registers with a
  one-time enrollment token, heartbeats, and claims jobs it has capacity and
  capabilities for. Hosts are added and removed without redeploying the control
  plane.
- **Sandbox.** Per-task container. Untrusted repository code runs here with no
  general internet egress — only the gateway, via a fixed-address relay.

## 7. Security boundaries — do not relax without review

- The sandbox reaches exactly one network destination: the gateway relay. It is
  a relay, not a proxy; the caller cannot choose a destination.
- Long-lived or cross-task credentials never enter the sandbox. Only
  short-lived, single-task, budget-capped, revocable grants do.
- Public multi-tenant use requires Kata/gVisor isolation, non-root, dropped
  capabilities, seccomp, and resource limits — plain Docker containers are for
  trusted internal repositories only.
