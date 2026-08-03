"""Replaying settled-terminal recovery markers after a restart.

Ported from the gateway's ``tests/servers/test_bootstrap.py`` at H4, where this
reconciler lived before the control plane became its own service. The sweep
that removed the gateway's agent tests checked each one against this repository
rather than assuming the move had carried it, and this property was not covered
here: a broker that is *down* must leave the marker set, so the next pass tries
again.

That is the whole point of a durable marker. Marking complete on a failed
resume loses the terminal silently — the job looks recovered, the user opens it,
and there is nothing there. It is worth a test precisely because the failure is
invisible from every side.

The one adaptation: the broker is resolved **per job** here, not once from the
environment. With a host pool there is no "the" broker — each job's retained
terminal lives on the machine that ran it — so the double is injected through
``broker_for_job`` rather than through the client factory.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cloud_agent import app as control_plane
from cloud_agent_host.workspace_broker_client import WorkspaceBrokerError

pytestmark = pytest.mark.asyncio


@pytest.fixture
def store() -> SimpleNamespace:
    return SimpleNamespace(
        list_terminal_resumes_pending=AsyncMock(return_value=["ajob_test"]),
        mark_terminal_resume_complete=AsyncMock(return_value=True),
    )


async def test_a_settled_terminal_resume_retries_until_the_broker_confirms(
    monkeypatch, store: SimpleNamespace
) -> None:
    """A refused resume leaves the marker set; the next pass clears it."""
    broker = SimpleNamespace(
        resume_settled_terminals=AsyncMock(
            side_effect=[WorkspaceBrokerError(503, "broker unavailable"), {"ok": True}]
        )
    )
    monkeypatch.setattr(
        "cloud_agent.broker_routing.broker_for_job",
        AsyncMock(return_value=broker),
    )

    await control_plane._reconcile_settled_terminals(store)
    store.mark_terminal_resume_complete.assert_not_awaited()

    await control_plane._reconcile_settled_terminals(store)
    store.mark_terminal_resume_complete.assert_awaited_once_with(job_id="ajob_test")
    assert broker.resume_settled_terminals.await_count == 2
    broker.resume_settled_terminals.assert_awaited_with("ajob_test")


async def test_a_deployment_with_no_broker_clears_the_marker(
    monkeypatch, store: SimpleNamespace
) -> None:
    """Otherwise "nothing to resume" is retried on every pass, forever.

    The distinction matters more after the split than before it: a job whose
    host has been removed from the pool resolves to no broker at all, and that
    is a normal end state rather than an outage to wait out.
    """
    monkeypatch.setattr(
        "cloud_agent.broker_routing.broker_for_job",
        AsyncMock(return_value=None),
    )

    await control_plane._reconcile_settled_terminals(store)

    store.mark_terminal_resume_complete.assert_awaited_once_with(job_id="ajob_test")
