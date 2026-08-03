# Migration record

This repository is a **clean snapshot** of the cloud agent feature extracted
from `HarvardMadSys/hybridInference`. It does not import that repository's git
history.

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
| Freeze commit | _not yet declared_ — see below |
| Manifest | `docs/agents/plans/cloud-agent-split-manifest.md` in the source repo |
| Plan | `docs/agents/plans/2026-08-03-cloud-agent-repo-split.md` in the source repo |

**The freeze commit is not yet set.** It is declared once these pull requests
land or close (task A1): #1147 (egress allowlist proxy), #1148 (MCP via the
gateway), #1167 (runner-host lock order), #1168 (Kata provisioning), #1170
(remote runner relay). Anything still open at freeze time has to be re-ported by
hand, so they are resolved first. Until then, migration tasks must not start —
they would copy files that are about to change.

When declared, record it here as `FREEZE_SHA` and regenerate the manifest
against it.

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
  billing ledger, and it is what enforces a job's budget.
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
| A | A1 — resolve open agent PRs | ⏳ blocked (human) | — |
| A | A2 — declare freeze | ⏳ blocked on A1 | — |
| C–H | contracts, moves, cutover | ⏳ not started | — |

Update this table as tasks land. It is the answer to "what is actually done" —
the plan describes intent, this describes reality.
