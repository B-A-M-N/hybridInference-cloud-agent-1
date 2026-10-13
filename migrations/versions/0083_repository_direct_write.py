"""Make sandbox repository writes an explicit per-repository authorization.

Revision ID: 0083_repository_direct_write
Revises: 0082_team_environments
"""

from alembic import op

revision = "0083_repository_direct_write"
down_revision = "0082_team_environments"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Persist disabled-by-default policies and revocable token records."""
    op.execute(
        """
        CREATE TABLE agent_repository_direct_write_policy (
            github_repository_id BIGINT PRIMARY KEY CHECK (github_repository_id > 0),
            repository_full_name TEXT NOT NULL,
            enabled BOOLEAN NOT NULL DEFAULT FALSE,
            revision BIGINT NOT NULL DEFAULT 0 CHECK (revision >= 0),
            updated_by TEXT,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute(
        """
        CREATE TABLE agent_repository_direct_write_tokens (
            token_hash TEXT PRIMARY KEY,
            github_repository_id BIGINT NOT NULL
                REFERENCES agent_repository_direct_write_policy (github_repository_id)
                ON DELETE CASCADE,
            policy_revision BIGINT NOT NULL,
            token_ciphertext TEXT NOT NULL,
            expires_at TIMESTAMPTZ NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute(
        "CREATE INDEX idx_agent_direct_write_tokens_repo_expiry "
        "ON agent_repository_direct_write_tokens (github_repository_id, expires_at)"
    )


def downgrade() -> None:
    """Remove direct-write authorization state."""
    op.execute("DROP TABLE agent_repository_direct_write_tokens")
    op.execute("DROP TABLE agent_repository_direct_write_policy")
