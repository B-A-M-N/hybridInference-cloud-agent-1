"""Fence GitLab OAuth token rotation with a generation and expiring lease.

Revision ID: 0050_gitlab_refresh_lease
Revises: 0049_admin_sandbox_trajectory
"""

from alembic import op

revision = "0050_gitlab_refresh_lease"
down_revision = "0049_admin_sandbox_trajectory"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Give each GitLab refresh one exclusive, reconnect-invalidated generation."""
    op.execute(
        """
        ALTER TABLE agent_gitlab_connections
            ADD COLUMN refresh_generation BIGINT NOT NULL DEFAULT 0,
            ADD COLUMN refresh_lease_expires_at TIMESTAMPTZ,
            ADD CONSTRAINT ck_agent_gitlab_connections_refresh_generation
                CHECK (refresh_generation >= 0)
        """
    )


def downgrade() -> None:
    """Remove GitLab refresh fencing without changing stored credentials."""
    op.execute(
        """
        ALTER TABLE agent_gitlab_connections
            DROP CONSTRAINT ck_agent_gitlab_connections_refresh_generation,
            DROP COLUMN refresh_lease_expires_at,
            DROP COLUMN refresh_generation
        """
    )
