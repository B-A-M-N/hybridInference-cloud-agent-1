"""Bind an enrollment token to the host name it is allowed to register.

Until this revision the token named no machine, so the *name* was chosen by the
machine redeeming it — and `register_host` upserts, revoking the incumbent's
credential and resetting its row to ``enrolling``. A single typo during a setup
therefore evicted a live, busy host from the claim pool: its credential stopped
authenticating, its heartbeat 401'd, and it kept running the attempts it had
already leased with no way to report them.

The name now travels with the token, which is minted by an authenticated
admin. Registration already requires a freshly issued token, so this costs the
operator nothing they were not doing anyway; it only moves the naming decision
to the side that is authenticated.

**Nullable, and the consume predicate compares it, so a row without one is
unusable.** Making it ``NOT NULL`` would fail on any database holding tokens
issued before this revision, and those are exactly the rows that must stop
working: a token that names no host is the thing this revision removes. They
expire within fifteen minutes anyway.

Revision ID: 0004_enroll_token_host
Revises: 0003_attempt_host
"""

from __future__ import annotations

from alembic import op

revision = "0004_enroll_token_host"
down_revision = "0003_attempt_host"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add the host name an enrollment token authorizes."""
    op.execute("ALTER TABLE agent_host_enroll_tokens ADD COLUMN IF NOT EXISTS host TEXT")


def downgrade() -> None:
    """Drop the binding again.

    Tokens outlive this by at most their TTL, so nothing is lost — but every
    row that remains becomes redeemable under any name again, which is the
    behaviour this revision exists to end.
    """
    op.execute("ALTER TABLE agent_host_enroll_tokens DROP COLUMN IF EXISTS host")
