# Migration record

This repository is a **clean snapshot** of the cloud agent feature extracted
from `HarvardMadSys/hybridInference`. It does not import that repository's git
history.

## Licence and provenance

This repository is **MIT**, carrying the source repository's licence text and its
copyright line — `Copyright (c) 2026 Harvard SEAS` — unchanged.

It was briefly created as Apache-2.0. That was wrong twice over. Apache-2.0 can
*contain* MIT code, but only while retaining the MIT notice, which a bare licence
swap drops. More importantly the copyright holder is Harvard SEAS, so
relicensing is not a decision this project can make on its own. Keeping MIT
avoids the question entirely and is the least surprising outcome for anyone who
already holds a copy of the original.

If a future relicence is genuinely wanted, it needs the copyright holder's
agreement first, not a change to this file.

## Why the history was not carried over

The source repository is private and its history is not a publication artifact —
it contains deployment details, internal hostnames, and credential-rotation
churn from a live service. Filtering that history well enough to publish is
strictly harder than starting clean, and a filter that is only *mostly* correct
publishes exactly the commits it failed to catch. Provenance is preserved in
this file and in commit messages instead.

## Source

| | |
|---|---|
| Source repository | `HarvardMadSys/hybridInference` (private) |
| Freeze commit | `764a6f97477504deb4542e1c28c3128ff9601c94` (2026-08-03) |
| Manifest | `docs/agents/plans/cloud-agent-split-manifest.md` in the source repo |
| Plan | `docs/agents/plans/2026-08-03-cloud-agent-repo-split.md` in the source repo |

Every migration task copies from this commit. Fetch a file with
`git show 764a6f97:<path>` rather than from the source repo's `dev`, which will
have moved on.

The freeze waited on five pull requests — #1147 (egress allowlist proxy), #1148
(MCP via the gateway), #1167 (runner-host lock order), #1168 (Kata
provisioning), #1170 (remote runner relay) — because anything unmerged at freeze
time would have to be re-ported by hand. All five landed on 2026-08-03, adding
13 files to the manifest.

One of them changed the shape of the split. The MCP proxy authenticates tool
calls with the same capability token and store fence as model calls, so it stays
on the gateway side and inference grants must describe MCP access as well as
models. See the manifest's coordination notes.

## Post-freeze drift register

The freeze promise is "copy from `764a6f97` and you copy what runs on dev".
Two files have drifted since — both formatting-only, verified by
`git diff 764a6f97..dev`:

| File | Drift | Consumer task |
|---|---|---|
| `apps/frontend/src/components/features/agents/IntegrationsView.tsx` | Prettier prose re-wrap, 2 lines | F2 |
| `tests/servers/test_agent_jobs_api.py` | two blank lines restored between test sections (the ruff fix that rode in with upstream #1177's merge) | E10 |

Rule for those tasks: copy from the freeze commit as usual, then run this
repo's formatter and confirm the result matches dev's copy — the diff against
dev must stay formatting-only. Any *new* drift discovered during a task gets a
row here before the task proceeds; **semantic** drift additionally needs
Murphy's sign-off, because it means the freeze was violated upstream.

## Deferred behaviour register

Differences against the source repo that are deliberate now and owed a fix by
a named task. Each entry lists the tripwire that keeps it from being
forgotten.

| Deferred | Owner | Tripwire |
|---|---|---|
| ~~Host logging is unconfigured until an entrypoint configures it.~~ | ~~**D6**~~ ✅ closed — `configure_process_logging()` is called from the runner's `main()` (the one sanctioned diff beyond the import rewrite), and `test_configure_process_logging_makes_info_reach_a_handler` is the required positive test. Import-time behaviour is unchanged and still pinned. | — |
| ~~`workspace_snapshot.py` moved in D3 without its test file.~~ | ~~**D6**~~ ✅ closed — `test_agent_workspace_snapshot.py` moved with the runner, unmodified. | — |
| ~~`workspace_paths.py` moved in D3 without dedicated coverage: its tests live inside `test_agent_workspace_broker.py` (D5).~~ | ~~**D5**~~ ✅ closed — the broker test file arrived in D5 and covers it. | — |

## Open decisions

Things a migration task surfaced that are **design calls, not mechanical
moves**, and so were deliberately not decided by the task that found them.

### The co-located runner overlay (found in D7)

`deploy/docker/docker-compose.agent-runner.yml` and `ops/deploy/agent_runner.sh`
did **not** move. They are not standalone: the compose declares a `backend:`
service and is composed *on top of* the gateway's own `docker-compose.yml`
(`-f docker-compose.yml -f docker-compose.agent-runner.yml`). The remote-runner
file says why in its own header — pointing the overlay at a machine with no
gateway does not fail, it *starts a gateway there*.

This repository has no `docker-compose.yml` base and no `backend` service, so
moving the pair as-is would land files that cannot run, and adapting them into
a standalone stack is a redesign that a migration task must not perform.

The two options, for whoever decides:

1. **Drop them.** A co-located runner becomes "the standalone remote-runner
   compose, with the tunnel pointing at localhost" — a configuration, not a
   second file. Cheapest, and it leaves one deployment shape to maintain.
2. **Keep a co-located topology as a first-class file here**, written against
   this repository's own services rather than the gateway's.

Their tests stay with them: `tests/unit/test_agent_deploy_config.py` asserts on
that compose, and `tests/unit/ops/test_agent_runner_preflight.py` executes
`agent_runner.sh`. Both are still in the source repo. The behaviours they pin
(internal egress network, read-only docker socket, non-root sandbox, kata
preflight) are **not currently covered here** — closing that gap is part of
whichever option is chosen. `tests/unit/test_deploy_paths.py` covers the moved
files' paths only, and says so.

Until this is decided, H4 must not delete the source repo's copies.

## Manifest corrections

Classification errors found while executing tasks. Fold these into the source
repo's manifest when it is next edited.

| File | Manifest says | Actually | Why |
|---|---|---|---|
| `apps/backend/serving/agent_jobs/terminal_coordination.py` | `move:host`, D5 | `move:control`, E6 | Its docstring says "Gateway-owned recovery"; its consumers at the freeze are `servers/bootstrap.py` and `servers/routers/agent_jobs.py` — control-plane code. It imports the broker *client* (`workspace_broker_client`, the caller side), not the broker. The route cases move with `test_agent_jobs_api.py` in E10; E6 ports the settled-terminal reconciliation case from `test_bootstrap.py`. |
| `deploy/docker/pi-freeinference`, `opencode-freeinference`, `kilo-freeinference`, `codex-requirements.toml` | absent from the manifest | `move:deploy`, D7 (moved to `deploy/sandbox/`) | `Dockerfile.agent-sandbox` `COPY`s all four. The manifest listed the Dockerfile but not its assets, so a literal reading of D7 would have produced an image that fails to build. |
| `deploy/docker/docker-compose.agent-runner.yml`, `ops/deploy/agent_runner.sh` (+ their two test files) | `move:deploy`, D7 | **undecided** — see Open decisions | They are an overlay on the gateway's compose stack, not a standalone deployment. |

## Upstream provenance

The cloud agent was built in the source repository between **2026-07-28** and
**2026-08-03** as issue #1041 and its follow-ups: 51 commits across 52 pull
requests.

<details>
<summary>Pull requests that produced the migrated code</summary>

#1041 (issue), #1054, #1057, #1061, #1064, #1073, #1089, #1090, #1096, #1098,
#1111, #1112, #1113, #1114, #1115, #1116, #1117, #1118, #1121, #1123, #1124,
#1125, #1126, #1129, #1131, #1133, #1134, #1136, #1138, #1139, #1141, #1142,
#1143, #1145, #1146, #1149, #1151, #1152, #1153, #1154, #1155, #1156, #1157,
#1158, #1159, #1160, #1161, #1162, #1163, #1165, #1166, #1169

Plus the pre-freeze set listed above: #1147, #1148, #1167, #1168, #1170.

</details>

## What stays in HybridInference

Not everything agent-shaped moves. These stay in the gateway because they are
about *inference*, not about *agents*:

- `api_logs.agent_job_id` and the cost/usage queries over it — this is the
  attribution ledger for reporting what a job spent. Monetary enforcement is
  the gateway's existing per-user quota; there is no per-job budget.
- The grant verification path (`model_auth.py` → a gateway-owned grants module):
  the gateway mints and verifies inference grants, so the signing secret never
  leaves it.
- User accounts, login, roles, and ban state, which this service consumes as an
  identity contract rather than a database.

## Landed here

| Phase | Task | Status | Commit |
|---|---|---|---|
| B | B1 — repository scaffold | ✅ landed | `96a2162` |
| B | B2 — CI and secret scanning | ✅ landed | this PR |
| B | B3 — migration record | ✅ landed | this PR |
| B | B4 — environment inventory | ✅ landed | this PR |
| A | A1 — resolve open agent PRs | ✅ done | 5 PRs merged upstream |
| A | A2 — declare freeze | ✅ done | `764a6f97` upstream |
| A | A3 — file manifest | ✅ done | upstream |
| C | C1 — identity JWKS endpoint (+ key rotation) | ✅ merged | upstream #1174 |
| C | C2/C3 — authorization code + token exchange | ✅ merged | upstream #1177 |
| C | C4 — /authorize consent page | ✅ merged | upstream #1179 |
| C | C5–C8 — inference grants, usage, contracts | ⏳ not started | needs Murphy design review |
| D | D1 — host package skeleton | ✅ done | landed with the B1 scaffold |
| D | D2 — sandbox, runtimes, egress (+proxy) | ✅ merged | this repo #2 |
| D | D3+D4 — workspace setup/paths/snapshot + patch gate | ✅ merged; D3 acceptance pending D6 | this repo #3; one PR because `workspace_snapshot` imports `patch_gate`; snapshot tests are owned by D6 above |
| D | D5 — workspace broker + browser | 🔍 in review | this repo #4; `terminal_coordination` reclassified to Phase E (see manifest corrections) |
| D | D6 — runner + broker client (+ logging entrypoint) | 🔍 in review | this repo #5, stacked on #4; closes both D6 register rows; D3's pending acceptance resolves here |
| D | D7 — host deploy files | 🔍 in review | this repo #6; co-located overlay undecided (see Open decisions) |
| E–H | control plane, web, host pool, cutover | ⏳ not started | — |

Update this table as tasks land. It is the answer to "what is actually done" —
the plan describes intent, this describes reality.
