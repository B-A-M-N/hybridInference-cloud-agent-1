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
| ~~`apps/frontend/src/components/features/agents/IntegrationsView.tsx`~~ | ~~Prettier prose re-wrap, 2 lines~~ ✅ reconciled in F2 — copied from the freeze, formatted with this repo's Prettier, and the result is byte-identical to `origin/dev`. Formatting-only confirmed. | — |
| ~~`tests/servers/test_agent_jobs_api.py`~~ | ~~two blank lines restored between test sections (the ruff fix that rode in with upstream #1177's merge)~~ ✅ reconciled in E10 — the freeze copy run through this repo's `ruff format` (0.15.12) is byte-identical to `origin/dev`'s copy. Formatting-only confirmed. | — |

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

| The store's `initialize()` is a no-op. It used to create the schema **and run five data backfills** (threads and thread messages for jobs predating them, the singleton runner-policy row, `thread_id`/`turn_no` on legacy jobs) on every boot. All forty statements moved verbatim to `migrations/versions/0001_baseline.py`. | **E6** must not restore startup DDL when it moves the bootstrap call site; **H1** must run `alembic upgrade head` against the imported database. ✅ it does — `migrate_connections.py` runs it before the import, because the store no longer creates these tables and nothing else would. **Correction to this row's reasoning:** the five backfills write `agent_threads`, `agent_thread_messages`, `agent_jobs` and `agent_runner_policy`, and H1 migrates *none* of those (DR5 leaves history behind). They are no-ops on the imported data. The upgrade is required for the schema, not for the backfills. | `tests/unit/test_migration_baseline.py` pins the statement inventory, the backfill count, and that the store executes no DDL. |

| The `model` token scope still exists in `tokens.py`, and is dead weight: it is signed by a root the gateway does not know, so it buys nothing. Kept so the moved tests run unmodified — which is what makes them evidence that re-keying changed nothing else. | **E9**: remove `SCOPE_MODEL` when the runner asks the gateway for a grant instead. | This row; `tests/unit/test_agent_job_tokens.py` still exercises both scopes, so deleting one fails loudly. |
| Re-keying control tokens invalidates every token in flight. An attempt holding one signed by the gateway's root is an attempt from before the migration. | ~~**H2**~~ ✅ written into the runbook — `docs/cutover.md` step 3 drains by *waiting*, because the runner has no drain mode: `run_forever` exits only on `KeyboardInterrupt`, and stopping it mid-job leaves the lease to expire and the attempt to be requeued into a queue nothing will serve. Still owed: executing it. | This row; `docs/cutover.md` § 3. |
| `test_agent_jobs_lifecycle.py::test_full_lifecycle_create_to_published_pr` is `xfail(strict=True)`. It posts to `/worker/jobs/{id}/publish/begin` and `/publish/complete`, which upstream commit `6de7248f` (PR #1098, 2026-07-29) **deleted** — a full-scope worker token could set `published_pr_url` from an arbitrary string, mark the job succeeded, and make the real publisher skip it forever. The test was never updated, because the `dbtest` tier does not run in CI on either side. It arrived here already red; E10 recorded it rather than repairing it, since restoring the routes restores the hole and rewriting the test to drive the publish worker is a rewrite a migration must not make. | **Whoever owns the publish path.** Rewrite the case against `publish_worker`, or delete it and say so. | `strict=True`: the moment anyone makes it pass, the suite fails. `test_agent_jobs_api.py::test_a_worker_cannot_write_the_publish_record` covers the property #1098 actually added, and passes. |

| `apps/backend/serving/agent_jobs/patch_gate.py` | `move:host`, D4 | **shared** — host *and* control plane | At the freeze it is imported by `runner.py` and `workspace_snapshot.py` (host) **and** by `publisher.py`, `publish_worker.py` and the API router (control plane). E5 imports it from `cloud_agent_host`, which works in one repository but means a control-plane image must ship the host package. A shared third package is the eventual right answer; that is a deployment-layout decision, not something a migration task should make. Whoever writes `deploy/control-plane/` needs to know either way. |

| `apps/backend/serving/servers/routers/agent_jobs.py` | "2,257 lines" | **2,550 lines** at the freeze commit | The count predates the five PRs that landed 2026-08-03. Anyone sizing E6 from the manifest would have under-estimated it by 13%. |
| `apps/backend/serving/schemas_admin.py` | absent from the manifest | `move:control`, E6 | `admin_hosts.py` cannot import without `AgentRunnerHost`, `ListAgentRunnerHostsResponse` and `SetActiveAgentRunnerHostRequest`. The three classes were copied verbatim into `cloud_agent/schemas.py`. Same shape of omission as D7's sandbox assets: the manifest listed a module but not what it imports. |

| **The host agent cannot enrol itself.** G2 ships both server halves — the admin endpoint that mints a one-time token and `POST /v1/agent/host/register` that redeems it — and the store, tokens and tests behind them. What is missing is the twenty lines in the *runner* that read `AGENT_ENROLL_TOKEN` at boot, POST it once, persist the returned credential, and heartbeat on a timer. It was left out because `runner.py` is a D6 file still in review, and because nothing breaks without it: a runner that only sets `AGENT_RUNNER_HOST` keeps claiming exactly as it did, unmetered and online. Until it exists, capacity limits, capability matching and revocation are reachable only by an operator calling the endpoints by hand. | **Whoever owns the host agent**, before a second machine is added in anger. | This row; `ENVIRONMENT.md` marks `AGENT_ENROLL_TOKEN` and `AGENT_HOST_CREDENTIAL` as unread; `grep AGENT_ENROLL_TOKEN host/` returns nothing. |

| **One broker token for the whole pool.** G5 routes each request to the broker on the host that ran the attempt, but host rows carry a URL and no credential — every broker still authenticates the single `AGENT_WORKSPACE_BROKER_TOKEN`. So a compromised host's broker token opens every other host's broker. Doing half of it here would have looked like the problem was solved. | **Whoever adds per-host secrets** — it is a distribution problem (issue at enrollment, rotate, deliver), not a routing one. | `test_every_broker_shares_the_one_token_today` in `tests/unit/test_broker_routing.py` asserts the current state, so the day it changes it changes visibly. |

## Discarded tests

Migration moves test files whole. When an artifact is *rewritten* instead of
moved, its tests cannot come along unchanged — so they are recorded here case
by case rather than quietly dropped, and each row says where the property went.

### `tests/unit/test_agent_visible_models.py` (E4)

The source walked the gateway's route registry. That registry does not exist in
this service, so all four cases test behaviour that stayed behind:

| Case | Where the property lives now |
|---|---|
| `test_role_gates_match_the_inference_path` | Gateway. Covered by `test_the_catalog_is_not_the_anonymous_one` in the source repo's `test_internal_lookups.py`, which pins that a `free` user sees strictly fewer models than a `pro` one. |
| `test_aliases_resolve_but_are_not_listed_twice` | Gateway. **And a behaviour change here:** `agent_model_resolvable` compared against aliases because it held the registry; it now compares against canonical ids, so an alias reads as unresolvable. Teaching `/internal/model-catalog` to return aliases is the fix, and it is a change to that contract. Recorded in the module docstring. |
| `test_unpublished_disabled_and_embedding_models_are_excluded` | Gateway — `agent_visible_models` there still applies published/role/disabled/chat-type filters before answering. |
| `test_visibility_resolver_override_wins_both_ways` | Gateway — the resolver is a gateway service and the catalog endpoint passes it. |

Replaced by `tests/unit/test_agent_visible_models.py` and
`tests/unit/test_gateway_client.py`, which test what this service can now get
wrong: faithfully reporting what the gateway said, and **failing closed** when
it cannot answer.

### `apps/frontend/src/lib/api/__tests__/client.test.ts` (F2)

`lib/api/client.ts` is the one file F2 rewrites rather than moves — F2's own
acceptance criterion forces it, because the `sessionStorage` access token lives
there and not in `agents.ts`. Its 260 lines of token/refresh machinery are gone,
so its test file cannot come along: every case drives `setAccessToken`,
`getAccessToken`, expiry-peeking, or the refresh-and-replay a cookie session has
no equivalent for.

Replaced by `web/src/lib/api/client.test.ts` (10 cases), which pins what the
rewrite is now responsible for: the request carries the cookie and **no**
`Authorization` header, a 401 announces the expired session **once** and is
**not** retried, an ordinary 5xx says nothing about the session, and
`jsonOrThrow` — carried over verbatim — still classifies errors the same way.

`jsonOrThrow`'s gateway-login branches (password reset, email verification,
duplicate signup) are unreachable here. They were kept rather than pruned: a
migration that edits logic is a migration that cannot be diffed, and the whole
point of moving the function untouched is that error classification is
demonstrably unchanged for every call `agents.ts` makes. Pruning is a
post-cutover PR.

### `mock.test.ts::never spends past the budget cap` (F2)

One case, deleted with the field it asserted on. `expect(job.spentUsd)
.toBeLessThanOrEqual(job.budgetUsd)` cannot survive `budgetUsd` being removed
from `types.ts`, and there is nothing left for it to mean: no per-task budget
exists to be exceeded. The sibling case in `adapt.test.ts` lost its budget
assertion and kept its PR-label one, and was renamed to match.

Actual spend is **not** affected and is still displayed and still tested —
`JobDetail`'s Spend / Input tokens / Output tokens / Model calls panel is
untouched, including the `hasLedger === false` case that says "Ledger
unavailable" rather than implying a job was free.

| Source-control credentials are encrypted with `AGENT_SOURCE_CONTROL_ENCRYPTION_KEY`, not the gateway-derived key they were written under. | ~~**H1**~~ ✅ **tooling written** — `deploy/migrate/migrate_connections.py` decrypts with the old material and re-encrypts with the new one. Still owed: *running* it (H2 step 5), and deciding which machine holds both keys for that one run. **Precision correction:** only `agent_gitlab_connections` has ciphertext (`access_token_ciphertext`, `refresh_token_ciphertext`, both `NOT NULL`). `agent_repo_grants` has no encrypted column at all and is a plain id-preserving copy. The third ciphertext column in the schema, `agent_oauth_states.code_verifier_ciphertext`, is correctly excluded — ten-minute TTL, single-use. | This row; `ENV_ENCRYPTION_KEY`'s docstring in `source_control.py`; `tests/unit/test_migrate_connections.py::test_copying_the_ciphertext_would_produce_a_broken_connection`, which fails if anyone turns the re-key back into a copy. |

| **The web app calls three session routes that do not exist yet.** `web/src/lib/api/session.ts` is written against `POST /v1/session/login`, `GET /v1/session/me` and `POST /v1/session/logout`. F1–F3 could not wait for them without leaving the app with no way in at all, so the browser half is complete and the server half is owed. Until then the app renders its sign-in spinner, redirects to the gateway, and fails at the callback. | **E8**. The shapes in `session.ts` are the contract: login takes `{identity_token}` and sets the HttpOnly cookie; `me` returns `{id, email, role}` (a 401 means "not signed in", not an error) and 401s must not be dressed up as 500s or `ProtectedRoute` will loop. | This row; `web/src/app/callback/page.test.tsx` and `ProtectedRoute.test.tsx` pin the browser side against a mocked BFF, so a mismatched server shape shows up as a red integration rather than a silent one. |
| **`sessionStorage` is still used in `web/`, and F2's acceptance criterion cannot be met literally.** The criterion is `rg sessionStorage web/` → no hits, standing in for "the access token is gone". The token *is* gone — `client.ts` was rewritten around the cookie and holds no credential. But `JobDetail.tsx` independently uses `sessionStorage` to carry a **draft prompt** across the navigation a rewind/fork performs (lines ~957, ~1070), and four cases in `JobDetail.test.tsx` assert on it. Removing it would be a logic edit to a moved file and would need those assertions weakened — migration rules 1 and 3. | **Whoever signs off F2.** Either accept the criterion as "no *credential* in web storage", which is satisfied, or schedule the draft-prompt change as its own post-cutover PR with its tests updated deliberately. | This row. `grep -rn sessionStorage web/src` returns exactly: `JobDetail.tsx` (draft prompt), `JobDetail.test.tsx` (its tests), and two comments in `client.ts`/`pkce.ts` explaining the removal. No credential among them. |
| **`budget_usd` is now inert.** The create API still accepts it, the store still writes it, and job records still return it — but **nothing in this repository checks it**, because enforcement was the per-task budget C5/C6 deliberately removed in favour of the gateway's per-user quota. A caller setting `budget_usd: 5` would reasonably believe it caps the job. It does not. Kept by E6 because removing a field is a logic change a migration task must not make. | **Whoever owns the API surface**, before this service takes real traffic. Either drop the field from `AgentJobCreateRequest`/`create_job`/the store column, or make it mean something again. F2 already omits it from the web client, which closes the UI half only. | This row; `grep budget backend/` returns storage and plumbing but zero comparisons. |

## The web app: what moved, what was recreated (F1–F3)

The plan sizes F2/F3 as a move of 39 files. It is, for the agent trees
themselves — but those files import eight things from outside them, and the
support surface had to come along or be re-authored. Recording the split
because "moved" and "rewritten" carry very different review obligations.

**Moved verbatim** (only route paths and the `budget_usd` removal differ; every
other byte matches `764a6f97`, and `IntegrationsView.tsx` additionally matches
`origin/dev`):

| From | To | Files |
|---|---|---|
| `apps/frontend/src/components/features/agents/` | `web/src/components/features/agents/` | 30 |
| `apps/frontend/src/app/agents/` | `web/src/app/(agents)/` | 8 |
| `apps/frontend/src/lib/api/agents.ts` + its test | `web/src/lib/api/` | 2 |
| `apps/frontend/src/lib/utils/errors.ts` | `web/src/lib/utils/errors.ts` | 1 |
| `apps/frontend/src/components/ui/Markdown.tsx` | same path | 1 |
| `apps/frontend/src/components/providers/ToastProvider.tsx` | same path | 1 |
| `apps/frontend/src/styles/globals.css` | same path | 1 |
| the frontend's `.eslintrc.json`, `.eslintignore`, `.prettierrc`, `.prettierignore`, `postcss.config.js`, `tailwind.config.ts`, `tsconfig.json`, `vitest.config.ts`, `vitest.setup.ts` | `web/` | 9 |

**Recreated**, because moving would have dragged in gateway concerns this
service does not have:

| File | Why not a move |
|---|---|
| `lib/api/client.ts` | The `sessionStorage` access token and its refresh loop are exactly what F2 removes. `jsonOrThrow` and `safeFetch` are carried over verbatim inside it. See the discarded-test record. |
| `config/env.ts` | The source read `NEXT_PUBLIC_API_BASE` and a deploy-target table for the gateway. Replaced by `NEXT_PUBLIC_AGENT_API_URL` plus the authorize URL, and the gateway's token endpoint is derived from the latter. |
| `components/providers/AuthProvider.tsx` | The source drove email/password login and token refresh. Sign-in happens at the gateway now; this asks `/v1/session/me` and nothing else. `hasRole` is carried over unchanged — the layout's internal-only gate depends on its exact ordering. |
| `components/providers/SiteConfigProvider.tsx` | The source fetched `/site-config` from the gateway. Build-time environment instead; the shape (`branding` + `features`) is unchanged because the moved layout destructures it. |
| `components/providers/index.tsx` | Same stack minus `QueryProvider` — no moved file uses TanStack Query, so shipping it would be a dependency with no caller. |
| `components/features/auth/ProtectedRoute.tsx` | The source pushed at its own `/login`. There is no login page here; an unauthenticated visitor goes to the gateway's `/authorize` with a PKCE challenge. |
| `app/layout.tsx`, `app/callback/page.tsx`, `lib/auth/{pkce,signin}.ts`, `lib/api/session.ts` | New. The document shell, the return leg of sign-in, and the PKCE machinery. |
| `next.config.js` | The source proxied a dozen gateway paths through Next rewrites. Nothing to rewrite here. |

**Not brought across:** `config/branding.ts` (137 lines of gateway branding —
the four values the layout actually reads became environment variables),
`lib/api/{auth,user}.ts`, `config/site-config.ts`, `QueryProvider`, and
`components/ui/{Header,SiteFooter,ErrorBoundary,Button,Card}`.

**Route re-rooting.** `/agents/*` became `/*`, as F3 requires. The moved pages
live in a `(agents)` route group so the moved `layout.tsx` stays a *nested*
layout with the shape its own test renders, while `app/layout.tsx` supplies the
`<html>`/`<body>` document Next requires at the root. Route groups do not appear
in URLs: the built routes are `/`, `/[jobId]`, `/archived`, `/connected`,
`/integrations`, plus `/callback` deliberately **outside** the group so it is
not behind `ProtectedRoute`.

The 22 route-string edits are a path change, not an assertion change — six test
expectations moved with the routes they name (`'/agents/ajob_1'` →
`'/ajob_1'`). The one place a *destination* genuinely changed is the header's
Docs Assistant and Dashboard links: they are gateway pages, so they became
absolute links to `branding.gatewayUrl` and disappear on a deployment that has
not been told where the gateway is. Leaving them as `<Link href="/dashboard">`
would have shipped two 404s.

## Review findings not yet resolved

An adversarial review of E10/F/G/H raised these. The P1 is fixed; the rest are
recorded so they are decisions rather than discoveries. Severities are the
reviewer's.

| Sev | Where | Finding |
|---|---|---|
| ~~P1~~ | `storage/store.py` | ~~A drained host silently returned to service: the reaper moved `draining → offline` and a heartbeat moved `offline → online`, so drain undid itself whenever the machine went quiet and came back.~~ ✅ **fixed** — `draining` is no longer a reaper candidate, pinned by `test_a_drained_host_stays_drained_when_it_goes_quiet_and_returns`. |
| ~~P3~~ | `app.py` | ~~Host reaping shared a `try` with lease reaping, so a host-reap failure skipped attempt recovery and stalled publishes for that pass.~~ ✅ **fixed** — separate `try`, since the shared one turned a reporting failure into a strictly worse outage. |
| P2 | `storage/store.py` | Registering under an existing host's name revokes the incumbent's credential and resets it to `enrolling`, taking a live busy host out of the pool. The enrol token names no host, so the name is the registering machine's to choose. Needs a decision: refuse a name in use, or make the token name its host. |
| P2 | `web/` (F2) | **Two moved files' test files were not brought across — 14 cases silently dropped**, and not recorded. `port test files whole` is the rule; this is exactly the failure it exists to prevent. |
| P2 | `web/` (F2) | The `budget_usd` removal is a **logic edit to 11 moved files** that also **deleted a test case and an assertion**. The manifest asks F2 to omit the field from the client; it does not license editing moved tests. Rule 3 (never weaken a test) applies regardless of the manifest note. |
| P2 | `schemas.py` | `HostRegisterRequest.broker_url` documents an env fallback that `broker_for_attempt` does not perform once a host row exists. Doc and code disagree; the doc is wrong. |
| P3 | `deploy/migrate/` | A `--apply` run that is then refused has already run `alembic upgrade head`, but reports "nothing was written". Schema *was* written. |
| P3 | `docs/gateway-removal.md` | Two accuracy breaks: a stated command result that is false in the repo the document is about, and a §1.4 path that does not exist at the freeze commit despite the header claiming every path was read there. |
| P3 | `web/src/lib/auth/pkce.ts` | The PKCE verifier uses one fixed `localStorage` key, which breaks concurrent sign-ins. The file's own comment says the key was chosen to dodge a `rg sessionStorage` grep — a guard worked around rather than satisfied. |
| P3 | `web/package.json` | The "pinned" Prettier is `^3.8.3`, a caret range. Only the lockfile holds 3.8.3, and 3.9.6 reformats a moved file, so a lockfile regeneration turns web CI red. |

## Open decisions

Things a migration task surfaced that are **design calls, not mechanical
moves**, and so were deliberately not decided by the task that found them.

### Two host registries, or one (found in G1)

The plan's G1 says to create `agent_hosts(host_id ulid pk, name unique, …)`
beside the `agent_runner_hosts` table upstream #1158 already shipped. It never
says what becomes of the old one, and G4's slot scheduler supersedes a claim
gate — the pin — whose behaviour E10 pins with ten ported tests.

Taken literally that produces two answers to "which hosts exist": two admin
surfaces, two lists an operator has to know to reconcile, and a claim gate
reading one while somebody edits the other. It also breaks the ten tests, which
under rule 3 is a parity break rather than a design change.

**Decided (2026-08-04): one registry.** `agent_runner_hosts` gains the pool
columns — status, labels, capabilities, slots, version, broker URL, heartbeat,
credential hash — and keeps its primary key. `agent_attempts.host_id` therefore
references `agent_runner_hosts(host)`.

What that buys, and what it costs:

- #1158's pin, its three endpoints and its ten tests keep meaning what they
  meant. The pin and the pool **compose**: the pin decides *which* host may
  claim, the pool decides *whether* that host is in a state to. Both gates run,
  both fail closed, and neither can silently undo the other.
- A host that joined by polling defaults to `online` with a null `slots_total`
  (unmetered) and empty capabilities, which is exactly the pre-G behaviour. No
  existing deployment has to enrol anything to keep working.
- The cost is one table holding two kinds of row — a *sighting*, whose name is
  a scheduling label anything with the dispatcher token can invent, and an
  *enrollment*, whose name is a credentialed identity. They are told apart by
  the `enrolled` flag, and `admin_hosts.py`'s docstring says which guarantee
  applies to which. A reviewer who dislikes that is disliking the right thing;
  the alternative was two tables that mean the same thing.
- The name `host_id` on `agent_attempts` is the plan's word for a column that
  holds a host *name*. Kept because the plan names it, and because renaming a
  foreign key later is cheaper than reconciling two registries.

If a ulid identity is genuinely wanted later, it is an added column plus a
backfill, not a second table.

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

**Decided (2026-08-04): option 1.** The overlay is dropped; the standalone
remote-runner compose is the only topology, and a co-located runner is that
compose with the tunnel pointing at localhost. H4 deletes all four source files.

Its guarantees are kept by porting the source test file **whole** rather than
by picking out the properties that seemed important:

- `tests/unit/test_deploy_config.py` — all 50 source assertions, re-pointed at
  the standalone compose. 45 pass unchanged; 3 are skipped with the Trusted-tier
  reason below; 2 were adapted where they keyed on the gateway's `backend`
  service, keeping the half that still exists.
- `tests/unit/test_deploy_security.py` — only what the port could not carry.
- `tests/unit/test_setup_kata_runtime.py` — the Kata preflight, driving the
  real script. D7 had missed this file entirely.

**Rule this produced: port test files whole, then adapt what fails.** A first
attempt hand-picked "the four properties that matter" and produced 13
assertions against the source's 50 — a 74% silent loss, in a file whose whole
job is to catch silent failures. Reading the source list is not the same as
porting it, and the compiler does not check prose. When a task drops an
artifact, its tests move first and get deleted individually with reasons, never
summarised.

**One functional gap that came with the decision.** The Trusted egress tier —
the Squid allowlist proxy and the `agent-egress-trusted` / `agent-egress-uplink`
networks — was declared **only** in the overlay. The standalone compose defaults
`AGENT_EGRESS_NETWORK_TRUSTED/CUSTOM/FULL` to empty and both tiers to
`platform_only`, so it never supported anything above `platform_only`; that was
acceptable while the overlay existed for the co-located case, and is not now.
`deploy/host/Dockerfile.agent-egress-proxy` therefore builds an image that no
compose file in this repository starts. A deployment needing setup-phase
registry access must add the proxy, its config-render service and the tiered
networks to the standalone compose. Not scoped here — it is a feature, not a
migration — but it is the reason the tier is currently unreachable rather than
merely unused.

### Which machine runs the connection migration (found in H1)

`deploy/migrate/migrate_connections.py` must hold **both** keys at once for the
length of one run: the gateway's, to decrypt, and this service's, to
re-encrypt. That is not an implementation choice — nothing else can read the
old rows — and it is the one place this repository brushes against AGENTS.md
rule 4.

What the code decides, and does not leave to cutover day:

- the old key is read from a **file** (or stdin), never from the environment
  and never from `argv`;
- `API_KEY_SECRET` in the script's own environment is **ignored**, with a note
  on stderr saying so, so it cannot become configuration by accident;
- nothing is persisted: no cache, no state table, no log line carrying key
  material or plaintext, and the source database is opened
  `default_transaction_read_only`.

What is **not decided**, and is an operator call rather than a coding one:

1. **On the gateway host**, with `AGENT_DATABASE_URL` pointing at the new
   database over the network. The old secret never leaves the machine that
   already holds it; the new key travels there for one run.
2. **On an operator workstation** with a route to both databases, both keys as
   files on a `tmpfs`, deleted afterwards.

Either satisfies rule 4's actual property — the gateway's secret must not end
up in the agent host's persistent configuration. Deciding it under a runbook
clock is how it ends up in an `.env`, so it is written down here now and
recorded in `deploy/migrate/README.md` § Where it runs. Whoever schedules H2
puts the answer in the cutover ticket.

## Manifest corrections

Classification errors found while executing tasks. Fold these into the source
repo's manifest when it is next edited.

| File | Manifest says | Actually | Why |
|---|---|---|---|
| `apps/backend/serving/agent_jobs/terminal_coordination.py` | `move:host`, D5 | `move:control`, E6 | Its docstring says "Gateway-owned recovery"; its consumers at the freeze are `servers/bootstrap.py` and `servers/routers/agent_jobs.py` — control-plane code. It imports the broker *client* (`workspace_broker_client`, the caller side), not the broker. The route cases move with `test_agent_jobs_api.py` in E10; E6 ports the settled-terminal reconciliation case from `test_bootstrap.py`. |
| `deploy/docker/pi-freeinference`, `opencode-freeinference`, `kilo-freeinference`, `codex-requirements.toml` | absent from the manifest | `move:deploy`, D7 (moved to `deploy/sandbox/`) | `Dockerfile.agent-sandbox` `COPY`s all four. The manifest listed the Dockerfile but not its assets, so a literal reading of D7 would have produced an image that fails to build. |
| `deploy/docker/docker-compose.agent-runner.yml`, `ops/deploy/agent_runner.sh` (+ their two test files) | `move:deploy`, D7 | **undecided** — see Open decisions | They are an overlay on the gateway's compose stack, not a standalone deployment. |
| `ops/setup/setup_kata_runtime.sh` | absent from the manifest | `move:deploy`, D7 (here as `deploy/host/setup_kata_runtime.sh`) | Found while writing H4's deletion list. It carries no "agent" in its name, so the A3 grep never matched it and **no regeneration of the manifest ever will** — same for its test, `tests/unit/ops/test_setup_kata_runtime.py`, which MIGRATION.md already caught by hand. Both must be deleted from the source repo at H4 off this row rather than off the manifest. |
| `ops/release/public_export.py:36` **and** `tests/unit/release/test_public_export_audit.py:161` | the manifest's coordination note names only the first | both hard-reference `apps/backend/serving/agent_jobs/patch_gate.py` | The audit test loads the gate by path to assert the export tool reuses its credential shapes. H4 breaks both, not one. Recorded in `docs/gateway-removal.md` § 3. |
| `apps/frontend/src/lib/api/__tests__/agents.test.ts` | absent from the manifest and from the plan's Appendix inventory | `move:web`, F2 | Same shape of omission as `schemas_admin.py` and D7's sandbox assets: the manifest lists `agents.ts` but not its test. 526 lines, 25 cases, and the **only** coverage of the SSE stream reader — frame parsing across chunk boundaries, cursor tracking, reconnect-without-a-gap. Ported whole. A literal reading of F2 would have shipped the streaming client uncovered. |

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

### `tests/servers/test_agent_jobs_api.py::test_sandbox_token_is_refused_on_owner_routes` (E10)

The only case of the 103 that did not port. It imports
`serving.servers.auth._is_inference_path` and asserts on it directly — no
request, no router, no client. The function classifies the gateway's *inference*
routes (`/v1/chat/completions`, `/v1/messages`, `/v1/embeddings`), and this
service serves none of them, so there is nothing here to point it at.
Re-implementing the predicate to give the test something to assert on would be
authoring production code inside a migration task.

| Property | Where it lives now |
|---|---|
| A model-scoped sandbox token cannot act as an owner on `/v1/agent/jobs` | **Ported and passing** — `test_sandbox_token_stays_out_of_owner_routes` drives it over the wire and asserts 403 `insufficient_scope`. |
| The same token cannot write job state on the worker surface | **Ported and passing** — `test_sandbox_token_cannot_write_job_state`. |
| Which URL prefixes count as billed inference | Gateway. It is a property of the gateway's route table, and the source file that owns it did not move. |

## E10's auth stand-ins

The router moved unmodified, so it still calls `get_current_user`,
`verify_api_key`, `verify_admin_access`, `has_role`, `looks_like_agent_token`,
`get_client_ip` and `log_admin_action` — all E7 placeholders that raise. The
ported tests supply them from `tests/support/e7_auth.py`, whose docstring is the
long version of this note.

Most of the suite is unaffected: 96 of the 102 ported cases stub
`authenticate_agent_owner` exactly as the source did, so the stand-ins only have
to make `require_agent_owner`'s role gate work. Six cases pop that override on
purpose and exercise the real dispatch, and for those it is worth being precise
about what a green run now means:

| Case | Still real coverage of this repo | Now pinning E7's contract instead |
|---|---|---|
| `test_browser_jwt_can_list_agent_jobs` | `_looks_like_browser_jwt` routes a compact JWT to the JWT verifier, and the route answers 200 | — (the token is minted by this repo's own `identity.mint_session`) |
| `test_expired_browser_jwt_is_not_reinterpreted_as_api_key` | 401, **not** a fallback to the API-key path — the regression the test exists for | the `"Token has expired…"` detail string, which the stand-in reproduces |
| `test_normal_api_key_still_authenticates_agent_owner` | a non-JWT bearer reaches the API-key path | `update_key_last_used` — the `api_keys` table stayed on the gateway |
| `test_sandbox_token_stays_out_of_owner_routes` | `looks_like_agent_token` keeps a worker token out of the JWT branch; 403 on the wire | the refusal is now flat ("no inference route here") rather than path-based |
| `test_claim_requires_the_dispatcher_credential` | the claim gate is closed to an ordinary caller | — |
| `test_dedicated_dispatcher_token_opens_exactly_one_door` | `verify_dispatcher_access`'s `AGENT_DISPATCHER_TOKEN` fast path, which is this repo's code | the fallback's refusal, via the stand-in |

**Tripwire.** When E7 lands, delete `tests/support/e7_auth.py` and let these
tests run against the real dependencies. The three right-hand entries above are
the acceptance list for that task: if E7 ships and the file is still here, the
port is pinning a stub.

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
| E | E10 — control-plane parity tests | 🔍 in review | 102 of 103 cases ported and passing (`test_agent_jobs_api.py` 102, `test_admin_agent_runner_hosts.py` 10) plus the `dbtest` lifecycle; 1 discarded and 1 `xfail(strict)` — both recorded above and both pre-existing upstream, not caused by the move |
| E | E6–E9 — API router, app shell, identity, sessions, grant client | 🔍 in review | this repo #11, stacked on #10; the control plane boots and serves 45 operations. **E7 is not actually done** — `deps.py`'s eight auth placeholders still raise, which is why E10 ships `tests/support/e7_auth.py` |
| E | E5 — source control + publishing | 🔍 in review | this repo #10, stacked on #9; cipher re-keyed to `AGENT_SOURCE_CONTROL_ENCRYPTION_KEY` |
| E | E4 — visible models over HTTP | 🔍 in review | this repo #9, stacked on #8; **rewritten, not moved** — see the discarded-test record below |
| E | E2+E3 — schemas, entitlement, control tokens (re-keyed) | 🔍 in review | this repo #8, stacked on #7; `AGENT_CONTROL_TOKEN_SECRET` replaces the gateway's root |
| E | E1 — store + Alembic baseline | 🔍 in review | this repo #7, stacked on #6; `initialize()` becomes a no-op, schema **and its five startup backfills** move to `0001_baseline` |
| D | D7 — host deploy files | 🔍 in review | this repo #6; co-located overlay undecided (see Open decisions) |
| F | F1+F2+F3 — Next.js scaffold, API client + components, pages as the root app | 🔍 in review | this repo; one PR because the moved pages do not type-check without the components and the components do not run without the scaffold. See "The web app: what moved, what was recreated" above. |
| H | H1 — connection migration tooling | 🔍 in review | `deploy/migrate/` — the two tables, re-keyed rather than copied, id-preserving, idempotent, row counts + a plaintext-derived checksum. 24 unit cases and 8 `dbtest` cases against two real databases. **Written, not run**: H2 step 5 runs it. |
| H | H2 — staging cutover runbook | 🔍 in review | `docs/cutover.md`. Written against the code, so it names the three creation routes to block at the edge (there is no feature flag) and says the runner has no drain mode. Four blockers listed that must land before it can be executed. |
| H | H4 — gateway removal checklist | 🔍 in review | `docs/gateway-removal.md`. Executed in the **source** repo after H3, not here. |
| H | H5 — operations doc | 🔍 in review | `docs/operations.md`, moved from the source repo's `docs/developer/agent-sandbox-operations.md`; deviations listed in its own closing section. |
| H | H3 — production cutover | ⏳ not started | human-led, Murphy's approval gate |
| G | G1–G7 — dynamic host pool | 🔍 in review | `0002_host_pool` + `0003_attempt_host`; enrollment, heartbeat, the status machine, host-aware claiming with atomic slots, per-host broker routing, drain/remove/revoke, and the host-loss chaos test. **Extends** `agent_runner_hosts` rather than adding the plan's second `agent_hosts` table — see Open decisions. The host-side client that would redeem an enrollment token is **not** written; see the deferred register. |
| E–H | remaining control plane | ⏳ not started | — |

Update this table as tasks land. It is the answer to "what is actually done" —
the plan describes intent, this describes reality.
