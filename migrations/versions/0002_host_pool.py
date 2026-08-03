"""Turn the runner-host list into a pool with capacity and a lifecycle.

``agent_runner_hosts`` arrived from upstream #1158 as a *sighting log*: a host
appeared by polling, and the only decision it supported was "which one of these
may claim". Phase G needs it to answer three more questions — is this machine
supposed to be taking work, how much can it take, and what can it run — so the
columns are added here rather than in a second table.

**One registry, not two.** The plan's sketch called for a new ``agent_hosts``
beside the old one; that would have left two answers to "which hosts exist",
two admin surfaces, and a claim gate reading one while an operator edited the
other. Extending the existing table keeps #1158's pin, its three admin
endpoints, and its ten tests meaning exactly what they meant before.

**Defaults are chosen so a host that has never heard of enrollment still
works.** ``status`` defaults to ``online`` and ``slots_total`` to NULL
(unmetered), which is precisely the pre-G behaviour: a machine that polls joins
the pool and claims. Enrollment (G2) is the *other* door, and it starts a host
at ``enrolling`` until it heartbeats.

``slots_used`` carries a non-negative CHECK. The store's release path already
refuses to decrement below zero, but the invariant belongs in the schema too:
it is the one number a concurrency bug would corrupt silently, and a constraint
turns "the pool slowly stops accepting work" into a loud failure at the write
that caused it.

There is deliberately **no** ``slots_used <= slots_total`` constraint. An
operator lowering ``slots_total`` on a busy host would otherwise be refused, or
worse, would break that host's running attempts when they try to release. The
claim gate enforces ``slots_used < slots_total`` at claim time, so an
over-subscribed host simply drains down to its new size.

Revision ID: 0002_host_pool
Revises: 0001_baseline
"""

from __future__ import annotations

from alembic import op

revision = "0002_host_pool"
down_revision = "0001_baseline"
branch_labels = None
depends_on = None

#: The five states a host can be in. Kept in one place in the schema and
#: mirrored by ``cloud_agent.host_pool.HOST_STATUSES``; the CHECK is what stops
#: a typo in application code from writing a state nothing will ever match.
_STATUSES = ("enrolling", "online", "draining", "disabled", "offline")

_UPGRADE: tuple[str, ...] = (
    # Pool membership.
    "ALTER TABLE agent_runner_hosts ADD COLUMN IF NOT EXISTS status TEXT NOT NULL "
    "DEFAULT 'online'",
    "ALTER TABLE agent_runner_hosts ADD COLUMN IF NOT EXISTS labels JSONB NOT NULL "
    "DEFAULT '{}'::jsonb",
    "ALTER TABLE agent_runner_hosts ADD COLUMN IF NOT EXISTS capabilities JSONB NOT NULL "
    "DEFAULT '[]'::jsonb",
    # NULL means unmetered, which is what every host that predates enrollment
    # is. A zero default would have stopped the whole existing pool dead.
    "ALTER TABLE agent_runner_hosts ADD COLUMN IF NOT EXISTS slots_total INTEGER",
    "ALTER TABLE agent_runner_hosts ADD COLUMN IF NOT EXISTS slots_used INTEGER NOT NULL "
    "DEFAULT 0",
    "ALTER TABLE agent_runner_hosts ADD COLUMN IF NOT EXISTS runner_version TEXT",
    "ALTER TABLE agent_runner_hosts ADD COLUMN IF NOT EXISTS broker_url TEXT",
    "ALTER TABLE agent_runner_hosts ADD COLUMN IF NOT EXISTS last_heartbeat_at TIMESTAMPTZ",
    # sha256 of the credential handed back at enrollment. The credential itself
    # is never stored: revocation is setting this to NULL.
    "ALTER TABLE agent_runner_hosts ADD COLUMN IF NOT EXISTS credential_hash TEXT",
    "ALTER TABLE agent_runner_hosts ADD CONSTRAINT agent_runner_hosts_status_check "
    "CHECK (status IN (" + ", ".join(f"'{status}'" for status in _STATUSES) + "))",
    "ALTER TABLE agent_runner_hosts ADD CONSTRAINT agent_runner_hosts_slots_used_check "
    "CHECK (slots_used >= 0)",
    "ALTER TABLE agent_runner_hosts ADD CONSTRAINT agent_runner_hosts_slots_total_check "
    "CHECK (slots_total IS NULL OR slots_total > 0)",
    # The claim gate reads only online hosts; the partial index keeps that scan
    # proportional to the pool that can actually take work.
    "CREATE INDEX IF NOT EXISTS idx_agent_runner_hosts_online ON agent_runner_hosts(host) "
    "WHERE status = 'online'",
    # Enrollment tokens. Hashed at rest and single-use: the row is the receipt,
    # not the secret, so a database read cannot enrol a machine.
    """
    CREATE TABLE IF NOT EXISTS agent_host_enroll_tokens (
        token_hash TEXT PRIMARY KEY,
        issued_by TEXT,
        expires_at TIMESTAMPTZ NOT NULL,
        consumed_at TIMESTAMPTZ,
        consumed_host TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_agent_host_enroll_tokens_expiry "
    "ON agent_host_enroll_tokens(expires_at)",
)


def upgrade() -> None:
    """Add pool membership to the host list and create enrollment tokens."""
    for statement in _UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    """Return the host list to a sighting log.

    The enrollment tokens go with it: they are short-lived by construction, so
    there is nothing here worth preserving across a downgrade.
    """
    op.execute("DROP TABLE IF EXISTS agent_host_enroll_tokens")
    op.execute("DROP INDEX IF EXISTS idx_agent_runner_hosts_online")
    for constraint in (
        "agent_runner_hosts_status_check",
        "agent_runner_hosts_slots_used_check",
        "agent_runner_hosts_slots_total_check",
    ):
        op.execute(f"ALTER TABLE agent_runner_hosts DROP CONSTRAINT IF EXISTS {constraint}")
    for column in (
        "status",
        "labels",
        "capabilities",
        "slots_total",
        "slots_used",
        "runner_version",
        "broker_url",
        "last_heartbeat_at",
        "credential_hash",
    ):
        op.execute(f"ALTER TABLE agent_runner_hosts DROP COLUMN IF EXISTS {column}")
