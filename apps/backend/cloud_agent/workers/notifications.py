"""One connection per process that listens for Postgres notifications.

Triggers notify when something a request is waiting on may have changed: a job
became claimable (``agent_work_available``, migration ``0029``), or a job stored
an event or changed state (``agent_job_events``, migration ``0030``). Each
channel has a receiver in this process that wakes whoever waits on it.

A notification is only a hint to read again; the rows stay the record. One sent
while nothing listened is gone, so every connect and every loss resets each
receiver, which wakes all of its waiters to read once and find out.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING, Any, Protocol

from cloud_agent.logging_utils import get_logger

if TYPE_CHECKING:
    from collections.abc import Sequence

    import asyncpg

logger = get_logger(__name__)

#: How often the idle listening connection is checked. One that died without
#: closing would otherwise leave every wait to run out its timeout unnoticed.
_PING_S = 30.0
_PING_TIMEOUT_S = 10.0
_RECONNECT_S = 5.0


class Receiver(Protocol):
    """What the listener needs from each channel's signal."""

    channel: str

    def notified(self, payload: str) -> None:
        """Wake whoever waits on what ``payload`` names."""

    def reset(self, *, listening: bool) -> None:
        """The connection opened or was lost: wake every waiter to read once."""


async def listen(dsn: str, receivers: Sequence[Receiver]) -> None:
    """Keep one connection listening on every receiver's channel until cancelled.

    A dedicated connection, because a pooled one is reset when released and
    would stop listening.
    """
    import asyncpg

    healthy = True
    while True:
        connection: asyncpg.Connection | None = None
        try:
            connection = await asyncpg.connect(dsn)
            lost = asyncio.Event()
            # Bound now: a late callback from an old connection must not end
            # the next one.
            connection.add_termination_listener(lambda _connection, lost=lost: lost.set())
            for receiver in receivers:
                await connection.add_listener(receiver.channel, _deliver_to(receiver))
            for receiver in receivers:
                receiver.reset(listening=True)
            if not healthy:
                healthy = True
                logger.info(
                    "agent_notification_listener_recovered",
                    extra={"event": "agent_notification_listener_recovered"},
                )
            while not lost.is_set():
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(lost.wait(), _PING_S)
                if not lost.is_set():
                    await asyncio.wait_for(connection.fetchval("SELECT 1"), _PING_TIMEOUT_S)
            raise ConnectionError("the listening connection closed")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Once per outage: waiters read on their own timeouts meanwhile,
            # so a report every reconnect would say nothing new.
            if healthy:
                healthy = False
                logger.warning(
                    "agent_notification_listener_failed",
                    extra={
                        "event": "agent_notification_listener_failed",
                        "error_type": type(exc).__name__,
                        "error": str(exc)[:300],
                    },
                )
        finally:
            if connection is not None and not connection.is_closed():
                # Not close(): that waits on the server, and the server may be
                # what stopped answering.
                connection.terminate()
        for receiver in receivers:
            receiver.reset(listening=False)
        await asyncio.sleep(_RECONNECT_S)


def _deliver_to(receiver: Receiver) -> Any:
    """The asyncpg callback that hands one channel's payload to its receiver."""

    def deliver(_connection: Any, _pid: int, _channel: str, payload: str) -> None:
        receiver.notified(payload)

    return deliver
