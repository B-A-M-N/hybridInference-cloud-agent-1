# Observability and per-tool resource accounting

- **Status:** draft v1 — for review (Murphy, Juncheng)
- **Date:** 2026-08-19
- **Companion:** `2026-08-17-session-resident-harness-threads-design.md`
  (continuity ladder, `continuity_mode` column, §9's promised admin
  aggregates); issue #57 (residency observability read path)
- **Model:** the DeepSeek harness session-telemetry seam
  (`docs/subsystems/session-telemetry.md`,
  `packages/session/session-telemetry*`, and the Agent Notes
  `2026-07-23-session-telemetry-otel-revival`,
  `2026-08-05-feedback-gated-session-telemetry`,
  `2026-08-10-telemetry-default-off` in that repository). §2 states exactly
  what is copied and what is deliberately not.

---

## 1. Problem

The platform is nearly blind. Inventory of everything that observes an agent
run today:

| Surface | What it holds | What it lacks |
|---|---|---|
| `agent_job_events` | Normalized + raw driver events per attempt, idempotent on `(attempt_id, client_seq)` | Written for UI replay; no aggregation, no export, no alerting; payloads unindexed |
| `agent_attempts` columns | `continuity_mode`, runtime/image version stamps, lease state | Facts exist per row; §9's promised aggregates (HOT hit-rate, export-failure rate, degradation reasons) were never built |
| Gateway `api_logs` | Tokens/cost per inference call, attempt- or thread-attributed (billing authority) | Lives in the gateway; no joined read path on our side (#57) |
| Squid egress proxy | **Already logs one line per CONNECT** — `ts`, `src`, allow/deny verdict, target host, `bytes=%<st` — to stdout | Nobody collects it; denied CONNECTs (a security signal) fall on the floor |
| `docker stats`, `journalctl`, ssh | Ad-hoc | Not recorded; runner prints are block-buffered and unsearchable (a real incident-response cost, already bitten during staging acceptance) |

There are zero metrics: no counter, no histogram, no dashboard, no alert on
the agent side. Consequences, concretely:

- Residency knobs ship blind. The TTL default, the per-host hot-sandbox LRU
  budget, and the #61 replica-dilution question are all tuned by guess; the
  design's own open questions 4–5 ask for data nobody records.
- Incidents are diagnosed by ssh archaeology. "Is the claim loop alive",
  "why did this thread degrade to replay", "which turn OOM-killed the host"
  all require shelling into two machines.
- Resource abuse is invisible. A tool call that pins 32 cores for ten
  minutes, or exfiltrates via an allowlisted domain, costs and risks real
  money and shows up nowhere.
- The ops-paper dataset (with Juncheng, EOY 2026) has no resource dimension
  and no continuity-outcome time series.

## 2. What we copy from the DeepSeek harness — and what we don't

Their telemetry subsystem was read in full, not skimmed; the principles below
each cite where they are pinned.

**Adopted:**

- **A1 — Projection, not a second truth.** Telemetry records are projections
  of the canonical stream; the canonical store is never rewritten, and the
  export may lose or duplicate records because consumers dedupe on the
  identity that rides along (`session-telemetry.md` §"The logical record").
  Our canonical stream per attempt is `agent_job_events` with
  `(attempt_id, client_seq)` — the dedupe key already exists.
- **A2 — Two channels: `ledger` and `ops`.** Ledger records mirror canonical
  events one-to-one and are summable after dedupe; ops records are signals
  with no log home (errors, lifecycle transitions), deliberately carry no
  sequence identity, and are alerted on, never summed.
- **A3 — Severity pre-mapped at capture** from the event's *own* outcome
  flags (`turn_failed`, a tool result's error flag), so a receiver alerts
  with zero configuration.
- **A4 — The boundary axiom.** Our capture ends at `emit()`. Batching,
  retry, queueing, and loss policy belong to the OpenTelemetry SDK and
  collector, configured — in their words — verbatim. We never hand-roll a
  delivery pipeline (their revival note records why: every self-built
  alternative was rejected).
- **A5 — A redaction stage between projection and export**, fail-closed per
  record, applying to the exported copy only. (Default flipped for us — see
  "not copied".)
- **A6 — Attribute minimalism.** Identity attributes only; the body carries
  the payload; nothing recoverable from the body is duplicated into
  attributes.
- **A7 — Stream-start projection.** They ship only the first
  `assistant/chunk` per (turn, step); we ship only the first `output_delta`
  per (attempt, turn) plus the terminal event. Sequence gaps on the wire are
  routine and never a loss signal.

**Not copied:**

- **Their consent surface.** DS telemetry is a *client-side* product
  exporting a user's own sessions to a third party, hence default-off,
  feedback-gated, anonymous-id machinery. We are the deployment observing
  our own hosted infrastructure — the events already land in our Postgres.
  What transfers is not consent but *export hygiene*: their waterfall ships
  no rules and exports full bodies by default; **ours defaults to
  deny-body** (O9), because our export sink is shared operational tooling,
  not the user's own chosen backend.
- **The Cordis seam machinery.** One in-process service registry does not
  map onto our three processes (control plane, runner, sandbox). The seam
  reduces to: capture points in CP and runner, each ending at an `emit()`
  whose default backend is a no-op (O1).
- **Per-chunk ledger mirroring** — see A7; we already batch deltas.

## 3. Goal

> Every attempt tells its whole story in queryable form: **what happened**
> (ledger), **what it consumed** (CPU, memory, io, network — per attempt,
> per residency, and per tool call), and **what the platform did around it**
> (ops). Postgres keeps every fact; our own console renders the operational
> view; a Slack-webhook checker watches it. A turn that we failed to
> measure says so instead of reporting zeros.

Non-goals (v1): distributed tracing (correlation ids suffice; spans are an
OBS-4 candidate), log-aggregation overhaul, harness-native OTel passthrough
(claude-code can export its own metrics; OBS-4 candidate, verify against the
pinned CLI), any third-party dashboard or metrics stack (no Grafana, no
Prometheus, no collector — O1), and any attribution precision beyond the
honest window model of §6.

## 4. Decision record

- **O1 — Postgres is the only deployed backend; export is a seam, not a
  stack.** We deploy no Grafana, Prometheus, or collector — the gateway
  already removed Prometheus once, and this deployment gets no second
  metrics estate to operate. Every fact lives in Postgres; the admin
  surface is our own console reading a CP aggregate API; alerting is a
  CP-side checker posting to a Slack webhook. The DS boundary axiom is kept
  as a *seam*: capture ends at `emit()`, and the default emit backend is a
  no-op. Attaching an OTLP exporter later (if ad-hoc exploration ever
  justifies a standard stack) is configuration, not surgery (OBS-4).
- **O2 — Three signal classes.** DS's `ledger` and `ops`, plus one they
  don't have: **resources** — periodic samples rolled up into per-window
  facts. Resources are measurements, not events: they become metrics and
  rollup rows, never ledger entries.
- **O3 — Resource accounting is sandbox-scoped at source.** The runner
  samples each live sandbox at 1 Hz from the host side: cgroup v2
  (`cpu.stat` usage, `memory.current`/`memory.peak`, `pids.current`,
  `io.stat`) resolved from the container's PID, plus the container's veth
  byte counters. No agent-visible process is involved; the sandbox cannot
  see, block, or forge the sampler. Samples are tagged
  `(thread, residency_generation, attempt | idle)`.
- **O4 — Per-tool attribution is windowed, and honest about it.** Tool
  windows come from the driver event stream; boundary quality is a
  per-driver capability, `tool_boundaries: exact | message | none`
  (claude-code upgrades to `exact` via passive PreToolUse/PostToolUse hook
  markers, §6; codex and claude-without-hooks are `message`-level from
  `tool_use`/`tool_result` arrival; pi is `message` today with native
  `tool_execution_start/end` events available; kilo/opencode are `none`).
  Overlapping windows (parallel tool use) share their delta: every
  overlapped call records the shared delta plus `overlap_n`, and rollups
  keep `exclusive` and `shared` fields apart. A shared number is never
  presented as exact.
- **O5 — Network is classified at the choke points we own.** Two of three
  are *already exactly attributed*: inference (gateway `api_logs`, grant →
  attempt/thread) and MCP (relay token is per-attempt). Only generic egress
  needs windows: the Squid access log (extended with `%>st` upload bytes and
  `%tr` duration so a connection's window is reconstructable) is collected
  and attributed like any other windowed signal. The sandbox's veth totals
  bound the sum: `veth − (gateway + mcp + egress)` is published as
  `unclassified`, and its growth alerts — a hole in the byte ledger is a
  hole in the egress story. Denied CONNECT verdicts become a counter and an
  alert today, not just a stdout line. Windowed inference display for
  resident turns already works this way (§14 of the resident design brackets
  `api_logs` by attempt window), so this adds no new attribution philosophy.
- **O6 — Idle residency is a first-class series.** Workspace bytes (sampled
  at establish/release and daily), idle sandbox memory, and idle share of
  each host's budget — the data that prices the LRU budget, the TTL default,
  and #61.
- **O7 — Cardinality budget.** Metric labels are bounded enums only: host,
  runtime, continuity mode, floor event kind, egress verdict, tool *name*
  (bounded per runtime registry). `thread_id`/`attempt_id`/domain names
  never label a metric; they live in Postgres rows (and later exemplars).
  A lint-style test enforces the label allowlist.
- **O8 — Failure honesty.** Sampler or exporter death never fails a turn.
  Every attempt rollup carries `coverage` (fraction of the attempt window
  actually sampled) and `sampler_version`; a gap is recorded as a gap. This
  is DS's "seq gaps are routine, never a loss signal" applied to
  measurement.
- **O9 — Export redaction defaults to deny-body.** The ledger projection
  exports event type, severity, sizes, ids, and a bounded allowlist of
  scalar fields; tool arguments/results and prompt text never leave Postgres
  via telemetry. The redaction stage is the DS waterfall shape — orderable
  rules, fail-closed per record, exported copy only — with the default rule
  set present rather than empty.
- **O10 — Retention.** Squid raw log 7 d after parsing; Postgres rollups
  and parsed flow rows indefinite (paper dataset); raw 1 Hz samples are a
  runner-side ring buffer only — rollups are computed at window close and
  the samples discarded. One retention tier, no second estate (O1).

## 5. Architecture

```text
sandbox (docker, per residency)      runner host                        control plane host
─────────────────────────────       ───────────                        ──────────────────
harness + tool children              ResourceSampler (1 Hz, host-side    worker API (exists):
  [claude: hook markers ——►          cgroupfs + veth reads)               events, continuity,
   state mount, append-only]         │                                    + NEW resource rollups
                                     ├─► window rollup at turn/idle close ─► Postgres rows
squid egress                         │                                    agent_job_events (exists)
  access log (bytes/verdict) ──►     egress log parser ─────────────────► net flow rows
                                     │
                                     emit() seam (default no-op;          aggregate API ─► console admin pages
                                     OTLP-attachable later, O1)           alert checker ─► Slack webhook
gateway api_logs (exists, billing authority) ───────── joined by grant attribution / attempt window ──► #57 read path
```

The runner→CP path for rollups is the existing worker-report pattern
(`worker_report_continuity` is the precedent): a small authenticated POST at
window close, idempotent per `(attempt_id, kind)`. The emit() seam may be
lossy by contract; Postgres rows are the facts (O1).

## 6. The resource model (the new part)

**Sampling.** At sandbox launch the runner resolves the container's cgroup
directory (via its PID) and its veth peer once, then reads counters at 1 Hz
per live sandbox — file reads, microseconds each, zero sandbox involvement.
Counters are monotonic; deltas survive in-sandbox process churn. Samples
carry a monotonic timestamp and the host wall clock. Kata (if it returns) is
a different backend: the sampler declares what it can read per sandbox
runtime; an unreadable backend yields `coverage = 0`, honestly, not zeros.

**Windows.**

| Window | Boundaries | Rollup destination |
|---|---|---|
| Attempt | claim → terminal state (runner-local) | `attempt_resources` row + metrics |
| Tool call | per O4: hook markers (`exact`) or event arrival (`message`) | `tool_call_resources` rows |
| Idle residency | residency minus attempt windows | residency series (O6) |

**Per-tool mechanics.** For `message`-level drivers, a call's window is
[`tool_use` arrival, matching `tool_result` arrival] joined on
`tool_use_id` — includes model "thinking" time before execution starts, which
is why it is labelled `message`, not `exact`. For claude-code `exact`: the
sandbox image's settings gain PreToolUse/PostToolUse hooks that append one
JSON line `{tool, tool_use_id, phase, t_mono}` to an append-only file on the
state mount; the runner tails it. Hooks are **passive by contract**: log-only
command, short timeout, exit 0 always; a hook that cannot write must not
block the tool (contract test 7). Hook config lives in the sandbox image /
injected home — platform infrastructure, never the user's repository, and
patch-gated out of any diff like the rest of the injected home.

**Attribution.** A delta between two samples is assigned to the tool windows
open during that interval. One open window: exclusive. `n` overlapping
windows: each records the shared delta with `overlap_n = n`; sums over
`overlap_n > 1` are upper bounds by construction and are stored under
`shared_*` fields, never `exclusive_*`. In-harness compute between tool
calls (the model streaming, the CLI's own work) accrues to the attempt, not
to any tool — visible as `attempt − Σ exclusive`, which is itself a useful
series (harness overhead).

**Network per tool** is windowed the same way from parsed egress flows
(connection window = [log time − duration, log time]), and only for the
egress class; gateway/MCP bytes are exactly attributed already (O5) and join
by attempt, not window.

**Rollup rows** (Postgres, new; names final at review):

```text
attempt_resources: attempt_id PK, cpu_usec, mem_peak_bytes, mem_avg_bytes,
  io_read_bytes, io_write_bytes, net_rx_bytes, net_tx_bytes,
  net_gateway_bytes, net_mcp_bytes, net_egress_bytes, net_unclassified_bytes,
  sample_count, coverage, sampler_version, created_at

tool_call_resources: attempt_id, tool_use_id, tool_name, source(exact|message),
  started_at, ended_at, overlap_n_max,
  cpu_usec_shared, cpu_usec_exclusive, mem_peak_shared_bytes,
  net_egress_bytes_windowed, truncated bool
  — capped at 500 rows per attempt; the cap sets `truncated` and increments a
  counter (no silent caps)

egress_flows: attempt_id nullable (idle flows keep residency ids), ts_start,
  ts_end, target_host, verdict, bytes_in, bytes_out
```

**Costs, honestly.** 1 Hz × a handful of sandboxes is negligible CPU and no
sandbox-visible overhead. Postgres grows by one row per attempt, ≤500 per
attempt for tools, one per egress connection; all bounded, all indexed by
attempt. The hook file grows within a turn and is truncated at turn close.
The standing cost of having eyes is one checker loop, one aggregate API,
and a few console pages — zero new services (O1).

## 7. Signal catalog (initial; the allowlist test pins it)

**Aggregates** (served by the CP aggregate API to the console, watched by
the checker; bounded dimensions per O7): turn duration histogram ×
(runtime, continuity_mode); turns total × (terminal state, degradation
reason); continuity mode counter (the §9 aggregate, finally); checkpoint
export outcome + size histogram; residency establish/evict/takeover counters
× reason; claim-loop lag gauge; event upload queue depth; attempt cpu/mem/io/
net totals as histograms × runtime; egress verdict counter × (allowed |
denied); unclassified-bytes gauge; sampler coverage histogram; tool duration
histogram × (tool_name, source).

**Ops records** (alert-on): attempt terminal failures with error class;
checkpoint export failure (already an event — projected, not re-emitted);
`residency_unusable`; grant mint/link degradation; reaper actions; sampler
start/stop/gap.

**Ledger projection**: floor events + terminal events, deny-body per O9,
dedupe key `(attempt_id, client_seq)` carried in attributes.

**Alert seed set** (a CP-side periodic checker over Postgres/CP state,
posting to its own Slack webhook — the `alert_slack` pattern, deliberately
never wired into the status-monitor control plane): claim loop silent
> 2 min; turn
failure rate; export failure streak (design §6 says a thread quietly loses
cold resume — now it pages); denied-CONNECT burst; unclassified-bytes growth;
sampler coverage < 0.9 sustained; host hot-sandbox budget saturation.

## 8. Degradation matrix

| Situation | Behavior |
|---|---|
| Sampler crashes mid-turn | Turn unaffected; rollup ships with `coverage < 1`; ops record |
| Attached export backend down (if any) | emit() drops per SDK policy; Postgres facts, console, and checker unaffected (O1) |
| Hook file absent / harness without hooks | `source = message` windows; no error |
| Hook cannot execute | Tool proceeds (passive contract); coverage of `exact` windows only |
| Parallel tool calls | `overlap_n > 1`, shared fields only |
| Squid log rotation / parse gap | Flow gap; `unclassified` absorbs it; gap counter |
| Attempt with zero samples (sub-second turn) | `coverage = 0`, `sample_count = 0` — never fabricated zeros |
| Idle residency (no attempt) | Samples tagged idle; egress flows keep residency ids (idle egress should be ~0 — its growth alerts) |
| Clock skew runner↔CP | Windows are runner-local monotonic; only display joins use wall clock |

## 9. Contract tests

1. Cgroup deltas are monotonic and survive in-sandbox process churn
   (spawn/kill storm) without negative or lost usage.
2. Serial attribution: a turn whose Bash tool burns CPU attributes ≥ 90 % of
   the attempt's cpu delta to that tool's exclusive window.
3. Parallel calls: overlapping windows all carry `overlap_n`, and no shared
   delta appears in an `exclusive_*` field.
4. Byte ledger: veth totals ≥ gateway + mcp + egress sums; `unclassified`
   equals the residual and is exported.
5. Sampler killed mid-turn: attempt succeeds; `coverage < 1` recorded; ops
   record emitted.
6. Hook contract: a hook that hangs or fails never delays or blocks the tool
   call (pinned against the stamped CLI, like the capability matrix).
7. Denied CONNECT increments the verdict counter with the attempt attributed
   when one is live.
8. Dimension allowlist: no aggregate-API dimension (nor any attached
   exporter's label) is unbounded (lint-style walk).
9. Ledger export: tool arguments/results and prompt text never appear in an
   exported record (deny-body default), while types/sizes/ids do.
10. Rollup idempotency: re-POSTing a window rollup upserts, never doubles.
11. Idle series: an established residency with no live attempt still
    produces idle-tagged samples and zero attempt attribution.
12. `coverage = 0` (unreadable backend) is distinguishable from "measured
    zero usage" in both row and metric form.

## 10. Implementation plan

| Phase | Content | Note |
|---|---|---|
| **OBS-0** | The emit() seam with no-op default; CP alert checker (claim-loop silence, queue depth, heartbeat gaps) → Slack webhook; env vars into ENVIRONMENT.md + the wiring test | "We have a pulse." Zero new services, no schema changes |
| **OBS-1** | Severity mapping + ops records; CP aggregate API + console admin pages (continuity funnel, capacity water-line, consumption leaderboard, security) — the aggregates the resident design's §9 promised; full alert seed set in the checker | Closes the blind-tuning gap for residency knobs |
| **OBS-2** | ResourceSampler + attempt/residency windows + `attempt_resources` + veth totals; Squid logformat extension + flow parser + verdict alerts | First resource facts; security dividend (denied-CONNECT, unclassified-bytes) |
| **OBS-3** | Tool windows (`message` for all Tier-1) + claude-code hook `exact` upgrade + `tool_call_resources` + per-turn resource line in the thread UI (§11) | The per-tool ask, honest about precision |
| **OBS-4** | Candidates, each its own decision: attaching a standard metrics stack (OTLP → Prometheus/Grafana) behind the emit() seam if ad-hoc exploration demands it; claude-code native OTel passthrough (verify against pinned CLI); MCP relay byte counters; pi native `tool_execution_*` boundaries; spans; #57 gateway-join read path; gateway-side paper events (other repo) | Not committed by this doc |

Phases are ordered so each ships standalone value; OBS-2 and OBS-3 are where
the review should spend its skepticism (window semantics, caps, hook
contract).

## 11. Product surfaces

What a person actually sees. The DS harness's user-facing observability is
the transcript itself plus a sharing disclosure — verified: no user-visible
cost, resource, or diagnostics UI exists in its docs or packages. It is a
local tool; its user owns the machine and can run `htop` themselves. We are
hosted: the user cannot see the machine, so the surfaces below are new
product, not ports.

**End user (thread UI).** v1 settles per turn, at turn close:

- **Turn footer, one line:** continuity badge + duration + inference cost +
  CPU seconds + peak memory + network in/out + blocked-connection count.
  Example: `hot · 42s · $0.031 · CPU 18.2s · peak 1.2GB · net ↓34MB ↑2MB ·
  2 blocked`.
- **Expanded:** a per-tool table (tool, duration, CPU, peak mem, windowed
  net; parallel calls labelled *shared — upper bound*), and a network panel
  (per-domain bytes, allowed/denied verdicts, denied highlighted).
- **The "explain the weird" surfaces** — the highest-value part: a `replay`
  badge with a tooltip saying this turn was rebuilt from transcript and why
  (export failure / host move); the footer separating compute-slow from
  network-slow; the denied list answering "is it phoning home".
- **Honesty rules are product copy, not fine print:** shared numbers say
  shared; a turn with `coverage < 1` shows "resource data incomplete", never
  a fabricated small number; `message`-level windows are labelled
  approximate until the hook upgrade.

**Admin.** Console admin pages — the same chart components as the
user-facing turn views, pointed at the CP aggregate API; no third-party
dashboard (O1), and the same pages serve future org admins, who would never
be handed a Grafana anyway: health pulse (claim lag, queues), continuity
funnel (HOT hit-rate per host — #61's data), capacity (hot-sandbox budget
water-line, idle cost), consumption leaderboard (outlier turns), security
(denied-CONNECT bursts, unclassified-bytes growth). Slack alerts from the §7
checker. Per-attempt detail exposes coverage, sampler version, and
degradation reason — the support answer to "why did my thread replay".

**Alignment with DS, stated as product:** we adopt its three philosophies —
transcript fidelity (our floor contract guarantees a never-blank turn;
Tier-2 remains a raw tail, honestly below their bar), honest degradation
(they never degrade so never say; we degrade, so we badge it), and
say-what-you-record (their consent disclosure becomes our transparency
statement: events, resources, and egress are recorded; tool arguments and
prompt text never leave the database via telemetry).

**Doors opened, not committed:** runaway protection (live samples make "this
turn has burned 30 CPU-minutes" a promptable/limitable signal), org usage
reports, compute-based pricing or quotas.

## 12. Open questions (resolve during review / OBS-0)

1. Alert webhook destination and channel (its own webhook, `alert_slack`
   pattern; a third producer — never the status-monitor control plane).
2. veth resolution mechanics (host-side peer lookup at launch) — pick at
   implementation; pure detail.
3. Which runner-local signals the checker needs pushed to the CP (claim
   heartbeat exists; does sampler liveness ride the rollup POSTs or its own
   beat?).
4. Tool-name label bound for Tier-2/raw runtimes (unknown tool vocabularies
   — likely `other` bucket to protect O7).
5. Whether `message` windows for claude-code should subtract streaming time
   using per-block timing available in stream-json, or wait for hooks.
6. Postgres partitioning for `egress_flows` if volume demands it (start
   unpartitioned, telemetry decides).

---

*Sign-off: Murphy, Juncheng. Implementation rides one PR per the house rule,
with this document cherry-picked in — not merged separately.*
