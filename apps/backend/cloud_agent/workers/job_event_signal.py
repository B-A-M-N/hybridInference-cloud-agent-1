"""Wake an owner's event stream when its job stores an event or changes state.

Triggers notify ``agent_job_events`` with the job's id (migration ``0030``), and
the process's shared listener (``notifications``) hands it here. Only streams
of that job are woken; each then reads the events table from its own cursor.

A stream subscribes *before* it reads and consumes its wake-up *after* it waits,
so a notification that arrives while it is reading is still waiting for it:
nothing stored between a read and the next wait is left for the timeout.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

CHANNEL = "agent_job_events"


class JobEventSignal:
    """Per-job wake-ups for the event streams of this process."""

    channel = CHANNEL

    def __init__(self) -> None:
        """Start with nobody waiting and no connection listening yet."""
        self._waiting: dict[str, set[asyncio.Event]] = {}
        #: Whether a connection is listening now. Until it is, and after it is
        #: lost, nothing would wake a stream, so streams read on a short clock.
        self.listening = False

    @contextlib.contextmanager
    def subscribe(self, job_id: str) -> Iterator[asyncio.Event]:
        """Register a wake-up for ``job_id`` for as long as the block runs."""
        woken = asyncio.Event()
        self._waiting.setdefault(job_id, set()).add(woken)
        try:
            yield woken
        finally:
            waiting = self._waiting.get(job_id)
            if waiting is not None:
                waiting.discard(woken)
                if not waiting:
                    del self._waiting[job_id]

    @staticmethod
    async def wait(woken: asyncio.Event, timeout_s: float) -> bool:
        """Wait for a wake-up or the timeout; report which, and consume it."""
        try:
            await asyncio.wait_for(woken.wait(), timeout_s)
        except TimeoutError:
            return False
        finally:
            woken.clear()
        return True

    def notified(self, payload: str) -> None:
        """Something was stored for, or changed on, the job ``payload`` names."""
        for woken in self._waiting.get(payload, ()):
            woken.set()

    def reset(self, *, listening: bool) -> None:
        """The connection opened or was lost: every stream reads once to catch up."""
        self.listening = listening
        for waiting in self._waiting.values():
            for woken in waiting:
                woken.set()

    def subscribers(self, job_id: str) -> int:
        """How many streams wait on ``job_id``; for tests and diagnostics."""
        return len(self._waiting.get(job_id, ()))
