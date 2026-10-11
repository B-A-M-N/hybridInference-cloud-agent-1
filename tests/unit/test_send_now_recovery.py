"""A restarted control plane picks up a send-now that was still waiting.

The wait lives in one process. The row does not: ``sending`` with a turn named
on it is enough to start that wait again. Two callers cannot both inject, and
a row that is no longer that handoff is left where it is.
"""

from __future__ import annotations

import asyncio
import contextlib
from types import SimpleNamespace

import pytest

from cloud_agent.api import turns

JOB = "ajob_1"
MESSAGE = {
    "id": "apmsg_1",
    "thread_id": "athr_1",
    "user_id": "u1",
    "revision": 2,
    "prompt": "and mind the tests",
    "state": "sending",
    "send_now_job_id": JOB,
}


class _Conn:
    def __init__(self, held: set[str], message_id: str) -> None:
        self.held = held
        self.message_id = message_id
        self.closed = False

    async def close(self) -> None:
        self.closed = True
        self.held.discard(self.message_id)


class _Pending:
    """The queue and the session lock, without a database."""

    def __init__(self, messages: list[dict]) -> None:
        self.messages = messages
        self.held: set[str] = set()
        self.conns: list[_Conn] = []
        self.returned: list[tuple[str, int]] = []
        self.handed: list[tuple[str, int]] = []

    async def queue_state(self, *, thread_id: str, user_id: str) -> dict:
        return {"messages": list(self.messages)}

    async def list_sending_handoffs(self) -> list[dict]:
        return [
            dict(row)
            for row in self.messages
            if row["state"] == "sending"
            and row.get("send_now_job_id")
            and not row.get("send_now_handed_off")
        ]

    async def send_now_handoff_pending(self, *, message_id: str, revision: int) -> bool:
        return any(
            row["id"] == message_id
            and int(row["revision"]) == revision
            and row["state"] == "sending"
            and not row.get("send_now_handed_off")
            for row in self.messages
        )

    async def mark_send_now_handed(self, *, message_id: str, revision: int) -> bool:
        self.handed.append((message_id, revision))
        for row in self.messages:
            if (
                row["id"] == message_id
                and int(row["revision"]) == revision
                and row["state"] == "sending"
            ):
                row["send_now_handed_off"] = True
                return True
        return False

    async def try_acquire_send_now_lock(self, message_id: str) -> _Conn | None:
        if message_id in self.held:
            return None
        self.held.add(message_id)
        conn = _Conn(self.held, message_id)
        self.conns.append(conn)
        return conn

    async def return_to_queue(self, *, message_id: str, revision: int) -> None:
        self.returned.append((message_id, revision))


class _Store:
    def __init__(self, messages: list[dict] | None = None) -> None:
        self.job = {"id": JOB, "state": "queued", "current_attempt_id": None}
        self.pending = _Pending(list(messages if messages is not None else [dict(MESSAGE)]))
        self.pending_messages = self.pending
        self.conversation_queries = SimpleNamespace(get_job=self._get_job)

    async def _get_job(self, job_id: str) -> dict:
        return dict(self.job)


@pytest.fixture(autouse=True)
async def _reset_handoffs():
    turns._completed_send_now.clear()
    leftover = list(turns._hand_overs)
    for task in leftover:
        task.cancel()
    for task in leftover:
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
    turns._hand_overs.clear()
    yield
    turns._completed_send_now.clear()
    leftover = list(turns._hand_overs)
    for task in leftover:
        task.cancel()
    for task in leftover:
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
    turns._hand_overs.clear()


async def test_recovery_starts_a_handoff_for_a_sending_row(monkeypatch):
    monkeypatch.setattr(turns, "HAND_OVER_POLL_S", 0.01)
    handed = []

    async def hand_over(_store, *, thread_id, attempt_id, message):
        handed.append((thread_id, attempt_id, message["id"]))
        return None

    monkeypatch.setattr(turns, "_hand_over", hand_over)
    queued = {**MESSAGE, "id": "apmsg_2", "state": "queued", "send_now_job_id": None}
    untied = {**MESSAGE, "id": "apmsg_3", "state": "sending", "send_now_job_id": None}
    store = _Store([dict(MESSAGE), queued, untied])

    await turns.recover_send_now_handoffs(store, gateway=None, app_credentials=None, signal=None)

    assert handed == []
    assert len(turns._hand_overs) == 1
    store.job.update(state="running", current_attempt_id=7)
    await asyncio.wait_for(asyncio.gather(*list(turns._hand_overs)), 1)

    assert handed == [("athr_1", 7, "apmsg_1")]
    assert store.pending.conns[0].closed
    await turns.recover_send_now_handoffs(store, gateway=None, app_credentials=None, signal=None)
    await asyncio.sleep(0.05)
    assert handed == [("athr_1", 7, "apmsg_1")]


async def test_a_caller_that_loses_the_lock_does_not_inject(monkeypatch):
    entered = asyncio.Event()
    release = asyncio.Event()
    handed: list[int] = []

    async def hand_over(_store, *, thread_id, attempt_id, message):
        handed.append(attempt_id)
        entered.set()
        await release.wait()
        return None

    monkeypatch.setattr(turns, "_hand_over", hand_over)
    store = _Store()
    first = asyncio.create_task(
        turns.run_send_now_handoff(
            store,
            thread_id="athr_1",
            job_id=JOB,
            message=dict(MESSAGE),
            gateway=None,
            app_credentials=None,
            attempt_id=7,
        )
    )
    await asyncio.wait_for(entered.wait(), 1)
    try:
        second = await turns.run_send_now_handoff(
            store,
            thread_id="athr_1",
            job_id=JOB,
            message=dict(MESSAGE),
            gateway=None,
            app_credentials=None,
            attempt_id=7,
        )
        assert second.injected is False
        assert handed == [7]
    finally:
        release.set()
    first_result = await asyncio.wait_for(first, 1)
    assert first_result.injected is True
    assert handed == [7]
    assert store.pending.returned == []
    assert store.pending.held == set()
    assert all(conn.closed for conn in store.pending.conns)


@pytest.mark.parametrize(
    ("state", "revision"),
    [("queued", 2), ("sending", 9)],
    ids=["back-in-line", "revision-moved"],
)
async def test_a_row_that_is_no_longer_sending_is_left_alone(monkeypatch, state, revision):
    handed = []

    async def hand_over(_store, *, thread_id, attempt_id, message):
        handed.append(message["id"])
        return None

    monkeypatch.setattr(turns, "_hand_over", hand_over)
    current = {**MESSAGE, "state": state, "revision": revision}
    store = _Store([current])

    result = await turns.run_send_now_handoff(
        store,
        thread_id="athr_1",
        job_id=JOB,
        message=dict(MESSAGE),
        gateway=None,
        app_credentials=None,
        attempt_id=7,
    )

    assert result.injected is False
    assert handed == []
    assert store.pending.returned == []
    assert store.pending.conns[0].closed
    assert (MESSAGE["id"], MESSAGE["revision"]) not in turns._completed_send_now


async def test_a_restart_does_not_inject_a_message_the_host_already_accepted(monkeypatch):
    handed: list[str] = []

    async def hand_over(_store, *, thread_id, attempt_id, message):
        handed.append(message["id"])
        return None

    monkeypatch.setattr(turns, "_hand_over", hand_over)
    accepted = {**MESSAGE, "send_now_handed_off": True}
    store = _Store([accepted])

    await turns.recover_send_now_handoffs(store, gateway=None, app_credentials=None, signal=None)
    assert turns._hand_overs == set()

    # The caller still holds the dict from before the mark. The re-read under
    # the lock is what refuses it. An empty completion set is a new process.
    result = await turns.run_send_now_handoff(
        store,
        thread_id="athr_1",
        job_id=JOB,
        message=dict(MESSAGE),
        gateway=None,
        app_credentials=None,
        attempt_id=7,
    )

    assert result.injected is False
    assert handed == []
    assert store.pending.handed == []
    assert store.pending.conns[0].closed
    assert turns._completed_send_now == set()
