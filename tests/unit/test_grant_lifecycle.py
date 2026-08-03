"""Keeping a grant alive, and killing it — the fence (task E9).

After the split the gateway holds **no attempt or lease state at all**. It
cannot tell a live attempt from one the reaper superseded thirty seconds ago,
so it cannot refuse a renewal on those grounds. The only place that knows is
this service's store, which is why ``_renew_live_grants`` re-reads it every
pass instead of remembering what it minted.

That is the property this file exists for, and
``test_a_superseded_attempt_stops_being_renewed`` is the case that would be
worth writing even if every other one here were deleted: a renewal loop that
skipped the store would keep a zombie sandbox spending someone's quota, and
nothing anywhere — here or at the gateway — would notice.

The store's own SQL predicates are pinned separately, against a real database,
in ``tests/integration/storage/test_agent_grant_store.py``.
"""

from __future__ import annotations

from typing import Any

import pytest

from cloud_agent.app import _renew_live_grants, _revoke_dead_grants
from cloud_agent.gateway import GatewayError

pytestmark = pytest.mark.asyncio


class _AttemptStore:
    """A store modelled on the two predicates the real queries use.

    An attempt is renewable while it is ``running``; anything else — superseded
    by the reaper, finished by its worker — is revocable. ``take_revocable_``
    detaches as it hands over, exactly as the ``UPDATE ... RETURNING`` does, so
    a grant is never offered twice.
    """

    def __init__(self) -> None:
        self.attempts: dict[int, dict[str, Any]] = {}

    def add(self, attempt_id: int, grant_id: str | None, status: str = "running") -> None:
        self.attempts[attempt_id] = {
            "job_id": f"ajob_{attempt_id}",
            "grant_id": grant_id,
            "status": status,
        }

    def supersede(self, attempt_id: int) -> None:
        self.attempts[attempt_id]["status"] = "superseded"

    async def list_renewable_grants(self, *, limit: int = 200) -> list[dict[str, Any]]:
        return [
            {"attempt_id": aid, "job_id": row["job_id"], "grant_id": row["grant_id"]}
            for aid, row in sorted(self.attempts.items())
            if row["grant_id"] is not None and row["status"] == "running"
        ]

    async def take_revocable_grants(self, *, limit: int = 200) -> list[dict[str, Any]]:
        taken = []
        for aid, row in sorted(self.attempts.items()):
            if row["grant_id"] is not None and row["status"] != "running":
                taken.append(
                    {"attempt_id": aid, "job_id": row["job_id"], "grant_id": row["grant_id"]}
                )
                row["grant_id"] = None
        return taken


class _Grants:
    def __init__(self, *, renew_fails: bool = False, revoke_ok: bool = True) -> None:
        self.renewed: list[str] = []
        self.revoked: list[str] = []
        self.renew_fails = renew_fails
        self.revoke_ok = revoke_ok

    async def renew(self, grant_id: str) -> dict[str, Any]:
        self.renewed.append(grant_id)
        if self.renew_fails:
            raise GatewayError("gateway answered 404 for renew")
        return {"grant_id": grant_id}

    async def revoke(self, grant_id: str) -> bool:
        self.revoked.append(grant_id)
        return self.revoke_ok


# ── Renewal ────────────────────────────────────────────────────────────


async def test_a_live_attempts_grant_is_renewed() -> None:
    store, grants = _AttemptStore(), _Grants()
    store.add(1, "agr_1")
    store.add(2, "agr_2")

    await _renew_live_grants(store, grants)

    assert grants.renewed == ["agr_1", "agr_2"]


async def test_a_superseded_attempt_stops_being_renewed() -> None:
    """**The fence.**

    The gateway cannot make this decision — it has no attempt state — so a loop
    that renewed on a timer would keep the superseded attempt's sandbox alive
    and spending until its own TTL ran out, and every renewal would push that
    moment further away. Consulting the store every pass is what makes
    superseding an attempt actually end it.
    """
    store, grants = _AttemptStore(), _Grants()
    store.add(1, "agr_1")
    store.add(2, "agr_2")

    await _renew_live_grants(store, grants)
    assert grants.renewed == ["agr_1", "agr_2"]

    store.supersede(1)
    await _renew_live_grants(store, grants)

    assert grants.renewed == ["agr_1", "agr_2", "agr_2"], "the superseded attempt was renewed"


async def test_an_attempt_with_no_grant_is_not_renewed() -> None:
    """A deployment whose gateway cannot mint grants yet is a working one."""
    store, grants = _AttemptStore(), _Grants()
    store.add(1, None)

    await _renew_live_grants(store, grants)

    assert grants.renewed == []


async def test_a_refused_renewal_does_not_stop_the_pass() -> None:
    """One grant the gateway has forgotten must not strand every other job."""
    store, grants = _AttemptStore(), _Grants(renew_fails=True)
    store.add(1, "agr_1")
    store.add(2, "agr_2")

    await _renew_live_grants(store, grants)

    assert grants.renewed == ["agr_1", "agr_2"]


# ── Revocation ─────────────────────────────────────────────────────────


async def test_a_superseded_attempts_grant_is_revoked() -> None:
    store, grants = _AttemptStore(), _Grants()
    store.add(1, "agr_1")
    store.supersede(1)

    await _revoke_dead_grants(store, grants)

    assert grants.revoked == ["agr_1"]


async def test_a_finished_attempts_grant_is_revoked() -> None:
    """The terminal case: the worker reported, the attempt is over, and the
    capability it was carrying should not outlive it by its whole TTL."""
    store, grants = _AttemptStore(), _Grants()
    store.add(1, "agr_1", status="finished")

    await _revoke_dead_grants(store, grants)

    assert grants.revoked == ["agr_1"]


async def test_a_live_attempts_grant_is_never_revoked() -> None:
    store, grants = _AttemptStore(), _Grants()
    store.add(1, "agr_1")

    await _revoke_dead_grants(store, grants)

    assert grants.revoked == []


async def test_a_grant_is_only_offered_for_revocation_once() -> None:
    """The store detaches as it hands over, so a gateway that refuses does not
    cost a call every pass for the rest of the deployment's life. The TTL is
    the backstop — revocation only accelerates it."""
    store, grants = _AttemptStore(), _Grants(revoke_ok=False)
    store.add(1, "agr_1")
    store.supersede(1)

    await _revoke_dead_grants(store, grants)
    await _revoke_dead_grants(store, grants)

    assert grants.revoked == ["agr_1"]


async def test_a_revoked_grant_is_not_then_renewed() -> None:
    """The two passes compose: revocation detaches the link, and renewal reads
    the same column, so nothing can be in both sets."""
    store, grants = _AttemptStore(), _Grants()
    store.add(1, "agr_1")
    store.supersede(1)

    await _revoke_dead_grants(store, grants)
    await _renew_live_grants(store, grants)

    assert grants.revoked == ["agr_1"]
    assert grants.renewed == []
