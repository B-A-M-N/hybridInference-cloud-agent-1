"""Record retained-sandbox history and reviewed deletion intent.

Revision ID: 0040_admin_sandbox_trajectory
Revises: 0039_compute_defaults
"""

from alembic import op

revision = "0040_admin_sandbox_trajectory"
down_revision = "0039_compute_defaults"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add durable delete intent and an append-only lifecycle ledger."""
    op.execute(
        """
        ALTER TABLE agent_attempts
            ADD COLUMN host_capacity_epoch UUID
        """
    )
    op.execute(
        """
        ALTER TABLE agent_sandboxes
            ADD COLUMN host_capacity_epoch UUID,
            ADD COLUMN delete_requested_at TIMESTAMPTZ,
            ADD COLUMN delete_requested_by TEXT,
            ADD COLUMN delete_requested_host_capacity_epoch UUID,
            ADD COLUMN deleted_at TIMESTAMPTZ,
            ADD CONSTRAINT ck_agent_sandboxes_delete_request_actor
                CHECK ((delete_requested_at IS NULL) = (delete_requested_by IS NULL)),
            ADD CONSTRAINT ck_agent_sandboxes_delete_request_epoch
                CHECK (
                    (
                        delete_requested_at IS NULL
                        AND delete_requested_host_capacity_epoch IS NULL
                    ) OR (
                        delete_requested_at IS NOT NULL
                        AND host_capacity_epoch IS NOT NULL
                        AND delete_requested_host_capacity_epoch = host_capacity_epoch
                    )
                ),
            ADD CONSTRAINT ck_agent_sandboxes_deleted_after_request
                CHECK (deleted_at IS NULL OR delete_requested_at IS NOT NULL)
        """
    )
    op.execute(
        """
        CREATE INDEX idx_agent_sandboxes_admin_delete_due
        ON agent_sandboxes (host, delete_requested_at, id)
        WHERE delete_requested_at IS NOT NULL AND deleted_at IS NULL
        """
    )
    op.execute(
        """
        CREATE TABLE agent_sandbox_events (
            id BIGSERIAL PRIMARY KEY,
            sandbox_id TEXT NOT NULL REFERENCES agent_sandboxes (id) ON DELETE RESTRICT,
            generation BIGINT NOT NULL CHECK (generation > 0),
            event_type TEXT NOT NULL
                CHECK (char_length(event_type) BETWEEN 1 AND 128),
            from_state TEXT,
            to_state TEXT,
            source TEXT NOT NULL
                CHECK (source IN ('system', 'admin', 'host', 'migration')),
            actor_id TEXT,
            details JSONB NOT NULL DEFAULT '{}'::jsonb
                CHECK (jsonb_typeof(details) = 'object'),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute(
        "CREATE INDEX idx_agent_sandbox_events_sandbox "
        "ON agent_sandbox_events (sandbox_id, id DESC)"
    )
    op.execute(
        """
        CREATE FUNCTION agent_record_sandbox_lifecycle_event()
        RETURNS TRIGGER
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF TG_OP = 'INSERT' THEN
                INSERT INTO agent_sandbox_events (
                    sandbox_id, generation, event_type, to_state, source, details, created_at
                ) VALUES (
                    NEW.id, NEW.generation, 'created', NEW.state, 'system',
                    jsonb_build_object(
                        'snapshot_state', NEW.snapshot_state,
                        'container_present', NEW.container_id IS NOT NULL
                    ),
                    NEW.created_at
                );
                RETURN NEW;
            END IF;

            IF OLD.state IS DISTINCT FROM NEW.state THEN
                INSERT INTO agent_sandbox_events (
                    sandbox_id, generation, event_type, from_state, to_state, source
                ) VALUES (
                    NEW.id, NEW.generation, 'state_changed', OLD.state, NEW.state, 'system'
                );
            END IF;
            IF OLD.snapshot_state IS DISTINCT FROM NEW.snapshot_state THEN
                INSERT INTO agent_sandbox_events (
                    sandbox_id, generation, event_type, from_state, to_state, source
                ) VALUES (
                    NEW.id, NEW.generation, 'snapshot_state_changed',
                    OLD.snapshot_state, NEW.snapshot_state, 'system'
                );
            END IF;
            IF OLD.container_id IS DISTINCT FROM NEW.container_id THEN
                INSERT INTO agent_sandbox_events (
                    sandbox_id, generation, event_type, source, details
                ) VALUES (
                    NEW.id, NEW.generation, 'container_changed', 'system',
                    jsonb_build_object(
                        'previous_present', OLD.container_id IS NOT NULL,
                        'current_present', NEW.container_id IS NOT NULL
                    )
                );
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_agent_sandbox_lifecycle_event
        AFTER INSERT OR UPDATE OF state, snapshot_state, container_id
        ON agent_sandboxes
        FOR EACH ROW EXECUTE FUNCTION agent_record_sandbox_lifecycle_event()
        """
    )
    op.execute(
        """
        CREATE FUNCTION agent_reject_sandbox_event_mutation()
        RETURNS TRIGGER
        LANGUAGE plpgsql
        AS $$
        BEGIN
            RAISE EXCEPTION 'agent_sandbox_events is append-only';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_agent_sandbox_events_append_only
        BEFORE UPDATE OR DELETE ON agent_sandbox_events
        FOR EACH ROW EXECUTE FUNCTION agent_reject_sandbox_event_mutation()
        """
    )
    # Existing rows have no trustworthy transition history. Record exactly
    # what this migration observed instead of inventing a creation sequence.
    op.execute(
        """
        INSERT INTO agent_sandbox_events (
            sandbox_id, generation, event_type, to_state, source, details
        )
        SELECT id, generation, 'current_state_imported', state, 'migration',
               jsonb_build_object(
                   'snapshot_state', snapshot_state,
                   'container_present', container_id IS NOT NULL,
                   'original_created_at', created_at
               )
        FROM agent_sandboxes
        """
    )


def downgrade() -> None:
    """Remove lifecycle history and deletion intent."""
    op.execute("DROP TRIGGER IF EXISTS trg_agent_sandbox_lifecycle_event ON agent_sandboxes")
    op.execute("DROP FUNCTION IF EXISTS agent_record_sandbox_lifecycle_event()")
    op.execute("DROP TABLE IF EXISTS agent_sandbox_events")
    op.execute("DROP FUNCTION IF EXISTS agent_reject_sandbox_event_mutation()")
    op.execute("DROP INDEX IF EXISTS idx_agent_sandboxes_admin_delete_due")
    op.execute(
        """
        ALTER TABLE agent_sandboxes
            DROP CONSTRAINT IF EXISTS ck_agent_sandboxes_deleted_after_request,
            DROP CONSTRAINT IF EXISTS ck_agent_sandboxes_delete_request_epoch,
            DROP CONSTRAINT IF EXISTS ck_agent_sandboxes_delete_request_actor,
            DROP COLUMN IF EXISTS deleted_at,
            DROP COLUMN IF EXISTS delete_requested_host_capacity_epoch,
            DROP COLUMN IF EXISTS delete_requested_by,
            DROP COLUMN IF EXISTS delete_requested_at,
            DROP COLUMN IF EXISTS host_capacity_epoch
        """
    )
    op.execute(
        """
        ALTER TABLE agent_attempts
            DROP COLUMN IF EXISTS host_capacity_epoch
        """
    )
