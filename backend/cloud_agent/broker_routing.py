"""Which workspace broker holds this job's worktree.

Before the pool there was one answer, in one environment variable: a single
runner host, a single broker, `AGENT_WORKSPACE_BROKER_URL`. With several hosts
that variable is not merely incomplete, it is *wrong in a specific way* — every
file read, terminal keystroke and git status for every job goes to whichever
machine the control plane happens to be configured for, so a job running on
host B shows host A's worktree, or nothing at all. Silently, and only for the
jobs that landed on the wrong machine.

So the broker is resolved from the attempt: the attempt records its host, the
host records its broker.

**Two fallbacks, and the difference matters.**

- *No host on the attempt* — the pre-pool case, and E11's single-host setup —
  falls back to the environment. That is the configuration the env var was
  always describing, and it must keep working without anybody enrolling
  anything.
- *A host with no broker URL* does **not** fall back. It answers "no broker",
  which surfaces as the same 409 a deployment without a broker already
  produces. Falling back there would mean routing host B's request to host A's
  broker, and the failure mode of that is not an error — it is an answer about
  the wrong machine's disk.

**The token is still global.** Host rows carry a URL, not a credential; every
broker in the pool authenticates the same `AGENT_WORKSPACE_BROKER_TOKEN`. That
is what the phase-G task asked for and no more: per-host broker credentials are
a secret-distribution design (issue one at enrollment, rotate it, get it onto
the machine) rather than a routing change, and doing half of it here would look
like the problem was solved. Until then, a compromised host's broker token
opens every other host's broker.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

from cloud_agent_host.workspace_broker_client import (
    WorkspaceBrokerClient,
    workspace_broker_from_env,
)

if TYPE_CHECKING:
    from cloud_agent.storage.store import AgentJobStore

ENV_BROKER_TOKEN = "AGENT_WORKSPACE_BROKER_TOKEN"


def _client_for(host: dict[str, Any]) -> WorkspaceBrokerClient | None:
    """Build a client for one host row, or ``None`` when it cannot be reached."""
    url = (host.get("broker_url") or "").strip()
    if not url:
        return None
    token = (os.environ.get(ENV_BROKER_TOKEN) or "").strip()
    if not token:
        return None
    return WorkspaceBrokerClient(url, token)


async def broker_for_attempt(
    store: AgentJobStore | None, attempt_id: int | None
) -> WorkspaceBrokerClient | None:
    """Resolve the broker holding the worktree of ``attempt_id``.

    ``None`` for the attempt id means the caller has no attempt in hand — a
    job that never ran, or one whose current attempt was released — and that
    takes the environment fallback, exactly as it did before the pool existed.
    """
    if store is not None and attempt_id is not None:
        host = await store.host_for_attempt(attempt_id=attempt_id)
        if host is not None:
            return _client_for(host)
    return workspace_broker_from_env()


async def broker_for_job(
    store: AgentJobStore | None, job_id: str | None
) -> WorkspaceBrokerClient | None:
    """Resolve the broker for a job, via its current attempt's host."""
    if store is not None and job_id:
        host = await store.host_for_job(job_id=job_id)
        if host is not None:
            return _client_for(host)
    return workspace_broker_from_env()
