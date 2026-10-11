"""Lock-order regressions for native session-fork membership mutations."""

from __future__ import annotations

from typing import Any

from cloud_agent.storage.repositories.session_fork import _lock_fork_memberships


class _RecordingConnection:
    def __init__(self) -> None:
        self.query = ""
        self.args: tuple[Any, ...] = ()

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        self.query = query
        self.args = args
        return [
            {"thread_id": "athr_target", "resident_state": "live"},
            {"thread_id": "zthr_source", "resident_state": "live"},
        ]


async def test_fork_memberships_lock_together_in_group_lifecycle_order() -> None:
    conn = _RecordingConnection()

    locked = await _lock_fork_memberships(
        conn,
        sandbox_id="asbx_shared",
        source_thread_id="zthr_source",
        target_thread_id="athr_target",
    )

    normalized = " ".join(conn.query.split())
    assert "WHERE sandbox_id = $1 AND thread_id = ANY($2::text[])" in normalized
    assert "ORDER BY thread_id FOR UPDATE" in normalized
    assert conn.args == ("asbx_shared", ["zthr_source", "athr_target"])
    assert list(locked) == ["athr_target", "zthr_source"]
