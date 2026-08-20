"""Merge execution profiles with the resource/reasoning migration line.

Revision ID: 0015_merge_execution_profiles
Revises: 0013_execution_profiles, 0014_reasoning_effort
"""

from __future__ import annotations

revision = "0015_merge_execution_profiles"
down_revision = ("0013_execution_profiles", "0014_reasoning_effort")
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Merge the two migration lines without an additional schema change."""


def downgrade() -> None:
    """Split the migration lines without an additional schema change."""
