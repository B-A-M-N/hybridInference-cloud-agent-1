"""Record the grant an attempt runs under, and who used the admin surface.

Two unrelated columns' worth of schema, in one revision because they arrive
with one task (E9's grant wiring and E7's audit trail) and splitting them would
give the chain two revisions nobody can order meaningfully.

**`agent_attempts.grant_id`** is the link between an attempt and the capability
the gateway minted for it. It has to be durable, and it has to live *here*:
after the split the gateway holds no attempt or lease state, so it cannot
notice that an attempt was superseded. The renewal pass reads this column
joined to the attempt's own ``status`` and lease, which is what keeps a
superseded attempt from being renewed — the fence. A control plane that
restarted mid-job with the link only in memory would silently stop renewing
live grants and silently stop revoking dead ones.

Nullable, because an attempt claimed before this revision — or on a deployment
whose gateway cannot mint grants yet — is a legitimate row. ``NULL`` reads as
"no grant to renew or revoke", which is the correct answer for both.

**`agent_admin_audit_log`** is the trail behind ``log_admin_action``. The
gateway's table did not move, and the alternative was a documented no-op: this
service's admin surface revokes a machine's credential, retargets every future
claim and removes hosts from the pool, and a service that owns those and keeps
no record of who used them has no answer to the only question asked after an
incident.

``details`` is ``jsonb`` and deliberately free-form; the call sites are
disciplined about never putting a credential in it (the enroll-token endpoint
logs the expiry, never the token). ``admin_ip`` is ``TEXT`` rather than
``INET`` because it is best-effort and may be the string ``unknown``.

Revision ID: 0005_grants_and_audit
Revises: 0004_enroll_token_host
"""

from __future__ import annotations

from alembic import op

revision = "0005_grants_and_audit"
down_revision = "0004_enroll_token_host"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add the attempt's grant link and the admin audit trail."""
    op.execute("ALTER TABLE agent_attempts ADD COLUMN IF NOT EXISTS grant_id TEXT")
    # Partial: the renewal and revocation passes both filter on
    # ``grant_id IS NOT NULL``, and every attempt that ever finished keeps a
    # row forever — indexing the NULLs would index the whole history.
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_agent_attempts_grant ON agent_attempts(grant_id) "
        "WHERE grant_id IS NOT NULL"
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_admin_audit_log (
            id BIGSERIAL PRIMARY KEY,
            action TEXT NOT NULL,
            admin_ip TEXT NOT NULL,
            target_user_id TEXT,
            details JSONB,
            success BOOLEAN NOT NULL DEFAULT TRUE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_agent_admin_audit_created "
        "ON agent_admin_audit_log(created_at DESC)"
    )


def downgrade() -> None:
    """Drop the grant link and the audit trail."""
    op.execute("DROP INDEX IF EXISTS idx_agent_admin_audit_created")
    op.execute("DROP TABLE IF EXISTS agent_admin_audit_log")
    op.execute("DROP INDEX IF EXISTS idx_agent_attempts_grant")
    op.execute("ALTER TABLE agent_attempts DROP COLUMN IF EXISTS grant_id")
