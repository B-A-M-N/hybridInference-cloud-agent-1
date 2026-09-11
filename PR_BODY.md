# Whole-turn resource accounting: one member's execution window

Implements the design in `2026-09-11-admin-turn-resource-accounting-design.zh.md`.
The interface contract the three layers agree on is
[`docs/agents/design/turn-resource-accounting.md`](../blob/realtmxi/codex/turn-resource-accounting/docs/agents/design/turn-resource-accounting.md),
which is also the reviewer's map.

Admin → Runs can now answer: **what did this turn cost inside its sandbox, and
which of those numbers are actually measured.** Servers keeps owning host
utilisation; Inference Usage keeps owning model calls, tokens and spend. These
numbers are not converted to money and are not a billing input.

## What is measured

One user input is one turn; the stored unit is one **member activation**. The
window opens when the member's resident is ready and the turn lock is held,
and closes before the platform's own cleanup runs.

| Metric | Source | Semantics |
| --- | --- | --- |
| CPU | per-thread scheduler time, live censuses plus settled exits | sum over thread instances; may exceed wall-clock duration |
| Memory | sampled PSS per address space, once per thread group | peak of a complete cycle; not machine memory, not a sum of peaks |
| Storage I/O | kernel task I/O accounting | what the kernel charged the member's own tasks, not what the host disk did |

Deliberately **not** measured here: queueing, image pull, VM start, the
supervisor and observer, the web terminal, platform git/snapshot/publish,
post-turn cleanup, and network traffic. This is a member execution bill, not
the platform's cost of a task, and the UI says so in the panel.

## How double counting is prevented

Two sources describe the same window — a live census of what every task still
running has consumed, and the kernel's settled exit for a task that finished.
They are the same work seen twice, so the reducer charges each thread instance
the difference between consecutive trusted readings of its own counters and
lets that instance's settled exit close that range:

- a settled exit adds only what the readings had not already reached;
- a duplicate exit delivery adds nothing;
- a counter that moved backwards is refused rather than reconciled;
- a cycle that could not read every task can neither open nor close a range;
- an unreadable counter is `NULL`, never `0`, and a missing baseline is never
  repaired with zero.

The final CPU figure is the larger of the two floors, and is marked `partial`
with `estimates_diverged` when they disagree beyond the reconciliation
tolerance rather than quietly averaging them.

## Layers

- **Guest** (`deploy/sandbox/`): the observer grows a live census, asked for by
  fixed verbs from a second reviewed helper over its own root-only,
  credential-checked socket. It reads `/proc/<tid>/schedstat`,
  `/proc/<tid>/io` and `/proc/<pid>/smaps_rollup`. It uses procfs rather than
  a BPF task iterator, so it adds no kernel capability and leaves the eBPF
  program and its event ABI untouched. `--self-test` now proves the counters
  exist on the target kernel and prints a capability matrix.
- **Host** (`cloud_agent_host/member_resources.py`): the reducer, the sampler,
  and the window lifecycle at the turn boundary.
- **Control plane**: `POST /v1/agent/worker/jobs/{job_id}/turn-resources`, a
  new endpoint beside the legacy one with a cumulative snapshot body, a
  monotonic `report_seq`, and a fence evaluated against the locked attempt row
  and the current database clock.
- **Admin**: Runs list cells, the run detail panel, and one shared reducer so
  the list and the detail cannot disagree.

## Storage compatibility, both directions

Three nullable columns on `agent_attempt_resources`, no backfill. The legacy
writer never touches them; this writer never touches the legacy columns or the
`agent_tool_call_resources` / `agent_process_observations` child rows. When
this writer runs first it creates the row with a legacy-compatible base
(`shared-member-v1`, `coverage = 0`, null counters), which is the existing
honest statement "no container totals were attributed to this member". Old
turns show "not collected" rather than a fabricated zero.

## Attention: migration parent

`down_revision = "0005_queued_follow_ups"`, **not** `0029_identity_namespace`
as the original design assumed. This branch's chain is the rewritten baseline
plus four additive revisions, so `0029` does not exist here. If this lands on
a lineage that still carries the old `0006`–`0029` chain, **this file must be
renumbered and reparented to that head** — two heads or a missing parent fails
at deploy rather than in tests. `tests/unit/test_schema_baseline.py` pins the
linear chain on this branch.

## What still has to be proven on a real Kata guest

No part of this can be established without one. `--self-test` answers all of
it on the target image, and the implementation ships the probe rather than
assuming the answer:

- `/proc/<tid>/schedstat` field 0 is the same quantity `se.sum_exec_runtime`
  holds for a task that has just died;
- `/proc/<tid>/status` exposes `Tgid`, and one thread group has one
  `smaps_rollup` entry;
- `/proc/<tid>/stat` field 22 is a clock-tick start time and `USER_HZ` is 100;
- guest-root reads of another UID's `/proc` entries succeed with the reviewed
  capability set;
- `smaps_rollup` puts `Pss:` inside the first 16 KiB;
- the workspace filesystem attributes task I/O to the member. If it does not,
  `storage_io` stays `unavailable` with `filesystem_attribution_unverified`
  rather than reporting two zeroes.

The guest C compiles clean under `-Wall -Wextra -Werror` (verified with
syntax-only checks against stub headers; the image build uses Debian's
`libbpf-dev`). It has **not** been loaded on a kernel here.

## Verification

```
ruff format --check .                               218 files already formatted
ruff check --no-fix .                               All checks passed
pydocstyle cloud_agent cloud_agent_host             clean
pytest -q -m "not external and not dbtest"          2495 passed, 1 failed
cd web && npx tsc --noEmit                          exit 0
cd web && npx vitest run                            896 passed (63 files)
cd web && npm run lint                              clean
npx --prefix web prettier --check                   clean
```

The one failure is pre-existing and environmental, not code:
`tests/unit/test_chat_sandbox.py::test_retained_terminal_keeps_the_shell_in_the_docker_tty_process_group`
fails because this development sandbox denies `os.openpty()`
(`PermissionError: [Errno 1]`), which also reproduces on a bare
`python -c "import os; os.openpty()"`.

New test coverage, all green: 40 window-reducer tests, 34 read-reducer tests,
22 reporting/checkpoint tests, 8 store compatibility cases (dbtest), 6 endpoint
cases, 4 admin cases, 4 preflight capability cases, 27 + 9 frontend cases.

**The 8 `dbtest` store cases are collected but were not executed**: they need
Postgres and none is available here. They cover resend-as-duplicate, lower
sequence, equal sequence with different payload, `final` not demoted by
`collecting`, a second `measurement_id` refused, superseded and expired leases,
and both compatibility directions.

## Reviewer notes

- **I/O is expected to be `unavailable` until proven.** The doc's own escape
  hatch: if the guest filesystem cannot attribute workspace I/O to the member,
  the metric reports unsupported with a reason instead of a zero.
- **The preflight is report-only.** It parses and logs the guest's capability
  matrix but the host does not yet restrict itself to the advertised subset, so
  a missing capability surfaces as a per-window `partial`/`unavailable`. Making
  preflight fail or downgrade a host is a deployment-policy decision and is
  left to review.
- **No owner-facing UI.** By design; the field rides the existing owner
  telemetry response as `null` and is not shown there.
- **Absence is silent.** A shared turn with no summary posts nothing at all; a
  producer regression would not be visible except through the admin page. A
  metric or log line for that is worth adding and is not in this change.
- `source_mismatch` and `estimates_diverged` were added to the reason-code
  enum beyond the original design's list; both are explained in the contract.
