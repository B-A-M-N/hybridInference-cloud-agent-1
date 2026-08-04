"""Baseline: the agent schema — and its backfills — as the gateway had them.

Generated from ``AgentJobStore.initialize`` at the freeze commit
(``764a6f97``) — every statement it executed, in order, **verbatim**: types,
defaults, constraint names, and the ``ADD COLUMN IF NOT EXISTS`` trail that
followed each table.

**This is not only DDL, and that is deliberate.** Five of the forty statements
are data migrations the source ran on every startup: backfilling threads and
thread messages for jobs that predate them, seeding the singleton
``agent_runner_policy`` row, and setting ``thread_id``/``turn_no`` on legacy
jobs. On a fresh database they are no-ops. On the database this project
imports rows into (task H1) they are the difference between a working thread
view and an empty one. Do not "clean up" the INSERTs as leftovers — they are
load-bearing, and they are the reason this file is not named after DDL.

Not normalised, on purpose. A tidied baseline produces a schema that is
*similar* to the one holding live data, and the differences only surface at
cutover, on rows nobody can afford to lose. Tidying is a later migration with
its own review, not a side effect of a move.

Those redundant-looking ALTERs are why a migration tool was needed at all:
creating a table "if not exists" silently does nothing when the table exists
with the wrong columns, so every later change had to be re-stated as an ALTER.
From here on, Alembic owns that.

Revision ID: 0001_baseline
Revises:
"""

from __future__ import annotations

from alembic import op

revision = "0001_baseline"
down_revision = None
branch_labels = None
depends_on = None

# Verbatim, in the source's own order — load-bearing twice over: an index
# cannot precede its table, an ALTER cannot precede the CREATE it patches,
# and a backfill cannot precede the column it writes.
_STATEMENTS: tuple[str, ...] = (
    '\n                CREATE TABLE IF NOT EXISTS agent_threads (\n                    id TEXT PRIMARY KEY,\n                    user_id TEXT NOT NULL,\n                    repo TEXT NOT NULL,\n                    title TEXT NOT NULL,\n                    archived_at TIMESTAMPTZ,\n                    pinned_at TIMESTAMPTZ,\n                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),\n                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()\n                )\n                ',
    'ALTER TABLE agent_threads ADD COLUMN IF NOT EXISTS archived_at TIMESTAMPTZ',
    'ALTER TABLE agent_threads ADD COLUMN IF NOT EXISTS pinned_at TIMESTAMPTZ',
    "\n                CREATE TABLE IF NOT EXISTS agent_jobs (\n                    id TEXT PRIMARY KEY,\n                    thread_id TEXT,\n                    parent_job_id TEXT,\n                    turn_no INTEGER NOT NULL DEFAULT 1,\n                    user_id TEXT NOT NULL,\n                    repo TEXT NOT NULL,\n                    base_sha TEXT,\n                    task_prompt TEXT NOT NULL,\n                    setup_script TEXT,\n                    runtime TEXT NOT NULL,\n                    model TEXT NOT NULL,\n                    state TEXT NOT NULL DEFAULT 'queued',\n                    cancel_requested BOOLEAN NOT NULL DEFAULT FALSE,\n                    terminal_resume_pending BOOLEAN NOT NULL DEFAULT FALSE,\n                    current_attempt_id BIGINT,\n                    published_pr_url TEXT,\n                    published_commit_sha TEXT,\n                    detail TEXT,\n                    metadata JSONB,\n                    mcp_servers TEXT[],\n                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),\n                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()\n                )\n                ",
    'ALTER TABLE agent_jobs ADD COLUMN IF NOT EXISTS terminal_resume_pending BOOLEAN NOT NULL DEFAULT FALSE',
    'ALTER TABLE agent_jobs ADD COLUMN IF NOT EXISTS setup_script TEXT',
    'ALTER TABLE agent_jobs ADD COLUMN IF NOT EXISTS thread_id TEXT',
    'ALTER TABLE agent_jobs ADD COLUMN IF NOT EXISTS parent_job_id TEXT',
    'ALTER TABLE agent_jobs ADD COLUMN IF NOT EXISTS published_commit_sha TEXT',
    'ALTER TABLE agent_jobs ADD COLUMN IF NOT EXISTS turn_no INTEGER NOT NULL DEFAULT 1',
    'ALTER TABLE agent_jobs ADD COLUMN IF NOT EXISTS fork_source_job_id TEXT',
    'ALTER TABLE agent_jobs ADD COLUMN IF NOT EXISTS mcp_servers TEXT[]',
    "\n                INSERT INTO agent_threads (id, user_id, repo, title, created_at, updated_at)\n                SELECT id, user_id, repo,\n                       LEFT(SPLIT_PART(task_prompt, E'\n', 1), 160), created_at, updated_at\n                FROM agent_jobs\n                WHERE thread_id IS NULL\n                ON CONFLICT (id) DO NOTHING\n                ",
    'UPDATE agent_jobs SET thread_id = id, turn_no = 1 WHERE thread_id IS NULL',
    "CREATE INDEX IF NOT EXISTS idx_agent_jobs_queued ON agent_jobs(created_at) WHERE state = 'queued'",
    'CREATE INDEX IF NOT EXISTS idx_agent_jobs_user ON agent_jobs(user_id, created_at DESC)',
    'CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_jobs_thread_turn ON agent_jobs(thread_id, turn_no)',
    'CREATE INDEX IF NOT EXISTS idx_agent_jobs_user_repo ON agent_jobs(user_id, repo, created_at DESC)',
    "\n                CREATE TABLE IF NOT EXISTS agent_attempts (\n                    id BIGSERIAL PRIMARY KEY,\n                    job_id TEXT NOT NULL REFERENCES agent_jobs(id),\n                    attempt_no INTEGER NOT NULL,\n                    lease_owner TEXT NOT NULL,\n                    lease_generation BIGINT NOT NULL,\n                    lease_expires_at TIMESTAMPTZ NOT NULL,\n                    terminal_ready BOOLEAN NOT NULL DEFAULT FALSE,\n                    sandbox_id TEXT,\n                    base_sha TEXT,\n                    status TEXT NOT NULL DEFAULT 'running',\n                    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),\n                    finished_at TIMESTAMPTZ,\n                    UNIQUE (job_id, attempt_no)\n                )\n                ",
    'ALTER TABLE agent_attempts ADD COLUMN IF NOT EXISTS terminal_ready BOOLEAN NOT NULL DEFAULT FALSE',
    "CREATE INDEX IF NOT EXISTS idx_agent_attempts_expiry ON agent_attempts(lease_expires_at) WHERE status = 'running'",
    '\n                CREATE TABLE IF NOT EXISTS agent_job_events (\n                    id BIGSERIAL PRIMARY KEY,\n                    job_id TEXT NOT NULL,\n                    attempt_id BIGINT NOT NULL REFERENCES agent_attempts(id),\n                    seq INTEGER NOT NULL,\n                    event_type TEXT NOT NULL,\n                    payload JSONB,\n                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),\n                    UNIQUE (attempt_id, seq)\n                )\n                ',
    'CREATE INDEX IF NOT EXISTS idx_agent_job_events_job ON agent_job_events(job_id, id)',
    '\n                CREATE TABLE IF NOT EXISTS agent_thread_messages (\n                    id BIGSERIAL PRIMARY KEY,\n                    thread_id TEXT NOT NULL REFERENCES agent_threads(id),\n                    job_id TEXT NOT NULL REFERENCES agent_jobs(id),\n                    role TEXT NOT NULL,\n                    content TEXT NOT NULL,\n                    source_event_id BIGINT UNIQUE REFERENCES agent_job_events(id),\n                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()\n                )\n                ',
    'CREATE INDEX IF NOT EXISTS idx_agent_thread_messages_thread ON agent_thread_messages(thread_id, id)',
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_thread_messages_user_job ON agent_thread_messages(job_id) WHERE role = 'user'",
    "\n                INSERT INTO agent_thread_messages (thread_id, job_id, role, content, created_at)\n                SELECT thread_id, id, 'user', task_prompt, created_at\n                FROM agent_jobs\n                ON CONFLICT DO NOTHING\n                ",
    "\n                INSERT INTO agent_thread_messages\n                    (thread_id, job_id, role, content, source_event_id, created_at)\n                SELECT j.thread_id, e.job_id, 'assistant', e.payload->>'text', e.id, e.created_at\n                FROM agent_job_events e\n                JOIN agent_jobs j ON j.id = e.job_id\n                WHERE e.event_type = 'message'\n                  AND NULLIF(BTRIM(e.payload->>'text'), '') IS NOT NULL\n                ON CONFLICT (source_event_id) DO NOTHING\n                ",
    '\n                CREATE TABLE IF NOT EXISTS agent_job_artifacts (\n                    id BIGSERIAL PRIMARY KEY,\n                    job_id TEXT NOT NULL,\n                    attempt_id BIGINT NOT NULL REFERENCES agent_attempts(id),\n                    kind TEXT NOT NULL,\n                    content TEXT NOT NULL,\n                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),\n                    UNIQUE (job_id, attempt_id, kind)\n                )\n                ',
    '\n                CREATE TABLE IF NOT EXISTS agent_repo_grants (\n                    id BIGSERIAL PRIMARY KEY,\n                    user_id TEXT NOT NULL,\n                    installation_id BIGINT NOT NULL,\n                    account_login TEXT,\n                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),\n                    UNIQUE (user_id, installation_id)\n                )\n                ',
    'CREATE INDEX IF NOT EXISTS idx_agent_repo_grants_user ON agent_repo_grants(user_id)',
    "\n                CREATE TABLE IF NOT EXISTS agent_oauth_states (\n                    state_hash TEXT PRIMARY KEY,\n                    user_id TEXT NOT NULL,\n                    provider TEXT NOT NULL,\n                    code_verifier_ciphertext TEXT,\n                    expires_at TIMESTAMPTZ NOT NULL,\n                    consumed_at TIMESTAMPTZ,\n                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),\n                    CHECK (provider IN ('github', 'gitlab'))\n                )\n                ",
    'CREATE INDEX IF NOT EXISTS idx_agent_oauth_states_expiry ON agent_oauth_states(expires_at)',
    '\n                CREATE TABLE IF NOT EXISTS agent_gitlab_connections (\n                    id BIGSERIAL PRIMARY KEY,\n                    user_id TEXT NOT NULL UNIQUE,\n                    external_user_id BIGINT NOT NULL,\n                    username TEXT NOT NULL,\n                    display_name TEXT,\n                    web_url TEXT,\n                    access_token_ciphertext TEXT NOT NULL,\n                    refresh_token_ciphertext TEXT NOT NULL,\n                    expires_at TIMESTAMPTZ NOT NULL,\n                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),\n                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()\n                )\n                ',
    '\n                CREATE TABLE IF NOT EXISTS agent_runner_hosts (\n                    host TEXT PRIMARY KEY,\n                    last_worker_id TEXT,\n                    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),\n                    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW()\n                )\n                ',
    '\n                CREATE TABLE IF NOT EXISTS agent_runner_policy (\n                    id BOOLEAN PRIMARY KEY DEFAULT TRUE,\n                    active_host TEXT,\n                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),\n                    CONSTRAINT agent_runner_policy_singleton CHECK (id)\n                )\n                ',
    'INSERT INTO agent_runner_policy (id, active_host) VALUES (TRUE, NULL) ON CONFLICT (id) DO NOTHING',
    'ALTER TABLE agent_runner_hosts DROP COLUMN IF EXISTS is_active',
    'DROP INDEX IF EXISTS idx_agent_runner_hosts_one_active',
)


def upgrade() -> None:
    """Create the agent schema and run the source's startup backfills."""
    for statement in _STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    """Drop every table this baseline created.

    Reverse dependency order, and CASCADE: the source schema declares no
    foreign keys, so this ordering is the only expression of what references
    what, and it should not be trusted to be complete.
    """
    for table in (
        "agent_runner_policy",
        "agent_runner_hosts",
        "agent_gitlab_connections",
        "agent_oauth_states",
        "agent_repo_grants",
        "agent_job_artifacts",
        "agent_thread_messages",
        "agent_job_events",
        "agent_attempts",
        "agent_jobs",
        "agent_threads",
    ):
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
