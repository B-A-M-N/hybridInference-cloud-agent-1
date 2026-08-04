"""Record which machine an attempt ran on.

Two things need this column, and they need it for different lengths of time.

*Scheduling* needs it while the attempt runs: the slot a host lends to an
attempt is released when that attempt stops running, and the only durable
statement of "this attempt is holding one of that host's slots" is this column
plus the attempt's own ``status``. Deriving it from anywhere else would mean
the counter and the truth could disagree after a crash.

*Routing* (G5) needs it afterwards too: the workspace broker lives on the host
that ran the attempt, so answering "where is this job's worktree" is a lookup
through this column. That is why the column is not cleared on release — the
attempt's ``status`` says whether the slot is still held, and the host stays on
the row as history.

``ON DELETE SET NULL`` rather than ``RESTRICT``: #1158's "forget this host" is
deliberate operator housekeeping on a decommissioned machine, and it must not
start failing because some finished attempt from last month still points at it.
A forgotten host's attempts fall back to the single-host env broker, which is
the same answer they get for an attempt that never had a host at all.

Revision ID: 0003_attempt_host
Revises: 0002_host_pool
"""

from __future__ import annotations

from alembic import op

revision = "0003_attempt_host"
down_revision = "0002_host_pool"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add the nullable host reference to attempts."""
    op.execute("ALTER TABLE agent_attempts ADD COLUMN IF NOT EXISTS host_id TEXT")
    op.execute(
        "ALTER TABLE agent_attempts ADD CONSTRAINT agent_attempts_host_id_fkey "
        "FOREIGN KEY (host_id) REFERENCES agent_runner_hosts(host) ON DELETE SET NULL"
    )
    # Reconciling a host's slot count means counting its running attempts; the
    # partial index keeps that off a full scan of every attempt ever made.
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_agent_attempts_host ON agent_attempts(host_id) "
        "WHERE host_id IS NOT NULL"
    )


def downgrade() -> None:
    """Drop the host reference."""
    op.execute("DROP INDEX IF EXISTS idx_agent_attempts_host")
    op.execute("ALTER TABLE agent_attempts DROP CONSTRAINT IF EXISTS agent_attempts_host_id_fkey")
    op.execute("ALTER TABLE agent_attempts DROP COLUMN IF EXISTS host_id")
