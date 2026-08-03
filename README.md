# FreeInference Cloud Agent

Cloud coding agents that run in sandboxed containers on a pool of hosts, drive a
user's repository, and publish the result as a pull request.

Model calls go through the [FreeInference](https://freeinference.org) gateway.
This service depends on that gateway through exactly two HTTP contracts —
**identity** (who the user is) and **inference grants** (what a job may spend and
on which models). No shared database, no shared signing secret, no shared code.

> **Status: under construction.** The implementation is being extracted from the
> HybridInference monorepo. See [MIGRATION.md](MIGRATION.md) for what has landed
> and what has not.

## Architecture

```text
                 ┌─────────────────────────┐
   browser ─────▶│  web/  (Next.js + BFF)  │
                 └───────────┬─────────────┘
                             │ session cookie
                 ┌───────────▼─────────────┐      identity JWT (JWKS)
                 │  backend/cloud_agent    │◀──── inference grants ────┐
                 │  control plane          │      grant usage          │
                 └───────────┬─────────────┘                           │
                             │ claim / heartbeat / terminal            │
                 ┌───────────▼─────────────┐                  ┌────────┴────────┐
                 │  host/cloud_agent_host  │                  │   FreeInference │
                 │  runner + broker        │                  │   gateway       │
                 └───────────┬─────────────┘                  └────────▲────────┘
                             │ spawns                                  │
                 ┌───────────▼─────────────┐   scoped grant token      │
                 │  sandbox (Kata/gVisor)  │───────────────────────────┘
                 │  codex / claude / kilo  │
                 └─────────────────────────┘
```

Three deployment units, one repository:

| Unit | Path | Runs where |
|---|---|---|
| Web + BFF | `web/` | alongside the control plane |
| Control plane | `backend/cloud_agent/` | one service, owns the Postgres database |
| Host agent | `host/cloud_agent_host/` | every machine in the host pool |

## Repository layout

```text
backend/cloud_agent/   control plane: API, auth adapter, scheduler, publishing
host/cloud_agent_host/ runner, sandbox, runtimes, workspace broker
web/                   Next.js app (user UI + admin dashboard)
contracts/             OpenAPI mirrors of the two gateway contracts
migrations/            Alembic migrations for the agent-owned database
deploy/                control-plane / host / sandbox deployment
tests/                 unit, servers, integration (dbtest marker)
docs/                  operations and design docs
```

## Getting started

Prerequisites: Python 3.12+ and [uv](https://github.com/astral-sh/uv).

```bash
make setup-dev
make check
```

`make check` = format + lint + test. Every change must leave it green.

| Command | What it does |
|---|---|
| `make format` | `ruff format` then `ruff check --fix` |
| `make lint` | ruff format check, ruff lint, pydocstyle |
| `make test` | pytest, excluding `external` and `dbtest` |
| `make test-db` | the `dbtest` tier (needs Postgres) |

## Contributing

Read [AGENTS.md](AGENTS.md) first — it is the onboarding guide for both humans
and AI coding agents, and it carries the migration rules that are currently in
force.

Branch off `dev` as `<user>/<scope>/<name>`; pull requests target `dev`.

## License

[Apache-2.0](LICENSE)
