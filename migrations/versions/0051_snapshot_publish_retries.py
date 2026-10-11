"""Count bounded snapshot publication retries across process restarts.

Revision ID: 0051_snapshot_publish_retries
Revises: 0050_gitlab_refresh_lease
"""

from alembic import op

revision = "0051_snapshot_publish_retries"
down_revision = "0050_gitlab_refresh_lease"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Persist the retry budget on each immutable publication artifact."""
    op.execute(
        """
        ALTER TABLE agent_sandbox_snapshots
            ADD COLUMN publish_retry_count INTEGER NOT NULL DEFAULT 0,
            ADD CONSTRAINT ck_agent_sandbox_snapshots_publish_retry_count
                CHECK (publish_retry_count >= 0)
        """
    )


def downgrade() -> None:
    """Remove publication retry accounting without changing artifacts."""
    op.execute(
        """
        ALTER TABLE agent_sandbox_snapshots
            DROP CONSTRAINT ck_agent_sandbox_snapshots_publish_retry_count,
            DROP COLUMN publish_retry_count
        """
    )
