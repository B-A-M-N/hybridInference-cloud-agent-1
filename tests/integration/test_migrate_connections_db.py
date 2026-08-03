"""The connection migration against two real Postgres databases.

The unit tests pin the decisions; this pins the thing that can only be wrong
against a real server: that the rows land with their ids, that the re-keyed
credentials come back out through this service's own cipher, that a second run
writes nothing, and that the `BIGSERIAL` sequence was moved past the imported
ids — the failure that would otherwise surface as a duplicate-key error the
first time a user connects after cutover.

Two databases are created per test and dropped afterwards, neither of them the
shared test database. The migration reads one and writes the other, so a
single-database rehearsal would not exercise the part that matters; and the
target is created *empty*, because running `alembic upgrade head` against it is
part of the script's job rather than a precondition it may assume.

The source database is built from the baseline statements alone — that is the
gateway's schema verbatim, and it is deliberately not this repository's `head`:
the gateway never had the migrations this project has added since.
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from cloud_agent.source_control import SourceControlCipher
from tests.support.migrations import run_baseline_statements

pytestmark = pytest.mark.dbtest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "deploy/migrate/migrate_connections.py"

_ALLOWED_TEST_DB_PATTERN = "_test_"
OLD_MATERIAL = "gateway-side-key-material-for-tests"
NEW_MATERIAL = "agent-side-key-material-for-tests"


def _load_script() -> Any:
    spec = importlib.util.spec_from_file_location("migrate_connections", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


mc = _load_script()


def _db_config() -> dict[str, Any]:
    base_db_name = os.getenv("TEST_DB_NAME", "hybridinference_test_db")
    worker_id = os.environ.get("PYTEST_XDIST_WORKER", "master")
    test_db_name = base_db_name if worker_id == "master" else f"{base_db_name}_{worker_id}"
    if _ALLOWED_TEST_DB_PATTERN not in test_db_name:
        pytest.fail(
            f"SAFETY: TEST_DB_NAME='{test_db_name}' does not contain "
            f"'{_ALLOWED_TEST_DB_PATTERN}'. Refusing to run."
        )
    return {
        "host": os.getenv("TEST_DB_HOST", "localhost"),
        "port": int(os.getenv("TEST_DB_PORT", "5432")),
        "database": test_db_name,
        "user": os.getenv("TEST_DB_USER", "postgres"),
        "password": os.getenv("TEST_DB_PASSWORD", "postgres"),
    }


def _dsn(config: dict[str, Any], database: str) -> str:
    return (
        f"postgresql://{config['user']}:{config['password']}"
        f"@{config['host']}:{config['port']}/{database}"
    )


async def _recreate(config: dict[str, Any], name: str) -> None:
    import asyncpg

    admin = await asyncpg.connect(**{**config, "database": "postgres"})
    try:
        await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await admin.execute(f'CREATE DATABASE "{name}"')
    finally:
        await admin.close()


async def _drop(config: dict[str, Any], name: str) -> None:
    import asyncpg

    admin = await asyncpg.connect(**{**config, "database": "postgres"})
    try:
        await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    finally:
        await admin.close()


async def _seed_source(config: dict[str, Any], name: str) -> None:
    """Build the gateway's schema and write rows encrypted under its key."""
    import asyncpg

    pool = await asyncpg.create_pool(**{**config, "database": name}, min_size=1, max_size=2)
    try:
        await run_baseline_statements(pool)
    finally:
        await pool.close()

    old = SourceControlCipher(OLD_MATERIAL)
    expires = datetime.now(timezone.utc) + timedelta(hours=2)
    conn = await asyncpg.connect(**{**config, "database": name})
    try:
        await conn.execute(
            "INSERT INTO agent_repo_grants (id, user_id, installation_id, account_login) "
            "VALUES (41, 'user-a', 900, 'acme'), (42, 'user-b', 901, NULL)"
        )
        await conn.execute(
            """
            INSERT INTO agent_gitlab_connections
                (id, user_id, external_user_id, username, display_name, web_url,
                 access_token_ciphertext, refresh_token_ciphertext, expires_at)
            VALUES (7, 'user-a', 12345, 'user-a', 'User A', 'https://gitlab.com/user-a', $1, $2, $3)
            """,
            old.encrypt("gitlab-access-plaintext"),
            old.encrypt("gitlab-refresh-plaintext"),
            expires,
        )
    finally:
        await conn.close()


async def _fetch(config: dict[str, Any], name: str, query: str) -> list[dict[str, Any]]:
    import asyncpg

    conn = await asyncpg.connect(**{**config, "database": name})
    try:
        return [dict(row) for row in await conn.fetch(query)]
    finally:
        await conn.close()


@pytest.fixture
def migration(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """A seeded gateway database, an empty target, and the script's environment."""
    config = _db_config()
    source_name = f"{config['database']}_h1_src"
    target_name = f"{config['database']}_h1_dst"

    asyncio.run(_recreate(config, source_name))
    asyncio.run(_recreate(config, target_name))
    asyncio.run(_seed_source(config, source_name))

    key_file = tmp_path / "old-key"
    key_file.write_text(OLD_MATERIAL + "\n")

    monkeypatch.setenv(mc.ENV_SOURCE_URL, _dsn(config, source_name))
    monkeypatch.setenv(mc.ENV_TARGET_URL, _dsn(config, target_name))
    monkeypatch.setenv("AGENT_SOURCE_CONTROL_ENCRYPTION_KEY", NEW_MATERIAL)

    def target(query: str) -> list[dict[str, Any]]:
        return asyncio.run(_fetch(config, target_name, query))

    yield {"key_file": str(key_file), "target": target}

    asyncio.run(_drop(config, source_name))
    asyncio.run(_drop(config, target_name))


def test_a_dry_run_writes_nothing(migration, capsys) -> None:
    """The default mode reports the plan and leaves both databases alone.

    The target has no schema at all here, which is also the point: a dry run
    must not migrate it either.
    """
    exit_code = mc.main(["--old-key-file", migration["key_file"]])
    output = capsys.readouterr().out

    assert exit_code == mc.EXIT_OK
    assert "nothing was written" in output
    tables = migration["target"](
        "SELECT tablename FROM pg_tables WHERE schemaname = 'public'",
    )
    assert tables == []


def test_the_import_preserves_ids_and_re_keys_the_credentials(migration, capsys) -> None:
    """The whole point, end to end.

    A copied ciphertext would satisfy every other assertion here — right ids,
    right row counts, right username — and fail only on the decrypt.
    """
    exit_code = mc.main(["--old-key-file", migration["key_file"], "--apply"])
    report = capsys.readouterr().out

    assert exit_code == mc.EXIT_OK
    assert "MATCH" in report and "DIFFERS" not in report

    grants = migration["target"]("SELECT id, user_id, account_login FROM agent_repo_grants")
    assert sorted(row["id"] for row in grants) == [41, 42]
    assert {row["user_id"] for row in grants} == {"user-a", "user-b"}
    # NULL survives as NULL rather than becoming the empty string.
    assert [row["account_login"] for row in grants if row["id"] == 42] == [None]

    connections = migration["target"](
        "SELECT id, user_id, username, expires_at, access_token_ciphertext, "
        "refresh_token_ciphertext FROM agent_gitlab_connections"
    )
    assert [row["id"] for row in connections] == [7]
    assert connections[0]["username"] == "user-a"

    new_cipher = SourceControlCipher(NEW_MATERIAL)
    assert new_cipher.decrypt(connections[0]["access_token_ciphertext"]) == (
        "gitlab-access-plaintext"
    )
    assert new_cipher.decrypt(connections[0]["refresh_token_ciphertext"]) == (
        "gitlab-refresh-plaintext"
    )
    # And the stored bytes are genuinely new — a copy would still decrypt under
    # the *old* key, which is the thing that must no longer be true.
    with pytest.raises(Exception, match="could not be decrypted"):
        SourceControlCipher(OLD_MATERIAL).decrypt(connections[0]["access_token_ciphertext"])


def test_re_running_is_a_no_op(migration, capsys) -> None:
    """The acceptance criterion: run it twice, nothing moves the second time."""
    key_file = migration["key_file"]
    assert mc.main(["--old-key-file", key_file, "--apply"]) == mc.EXIT_OK
    first = capsys.readouterr().out

    assert mc.main(["--old-key-file", key_file, "--apply"]) == mc.EXIT_OK
    second = capsys.readouterr().out

    # Same checksums across both runs — the rows were not re-encrypted.
    assert "MATCH" in second and "DIFFERS" not in second
    for line in first.splitlines():
        if line.startswith("  source "):
            assert line in second
    for name, counts in (
        ("agent_repo_grants", (2, 2, 0, 0, 2, 0)),
        ("agent_gitlab_connections", (1, 1, 0, 0, 1, 0)),
    ):
        row = next(line for line in second.splitlines() if line.startswith(name))
        assert [int(value) for value in row.split()[1:]] == list(counts)


def test_the_id_sequence_is_moved_past_the_imported_rows(migration) -> None:
    """The failure that would otherwise wait for the first post-cutover connect."""
    assert mc.main(["--old-key-file", migration["key_file"], "--apply"]) == mc.EXIT_OK

    new_id = migration["target"](
        "INSERT INTO agent_repo_grants (user_id, installation_id) "
        "VALUES ('user-c', 902) RETURNING id"
    )
    assert new_id[0]["id"] > 42


def test_a_wrong_old_key_refuses_before_writing(migration, tmp_path, capsys) -> None:
    """Fail closed: nothing is written when the key does not open the rows."""
    wrong = tmp_path / "wrong-key"
    wrong.write_text("not-the-gateways-key")

    assert mc.main(["--old-key-file", str(wrong), "--apply"]) == mc.EXIT_CONFIG
    assert "did not decrypt" in capsys.readouterr().err
    # Not even the schema: the decrypt happens before the target is touched.
    assert migration["target"]("SELECT tablename FROM pg_tables WHERE schemaname = 'public'") == []


def test_history_is_left_behind(migration) -> None:
    """DR5, asserted rather than trusted: the job tables stay empty here."""
    assert mc.main(["--old-key-file", migration["key_file"], "--apply"]) == mc.EXIT_OK

    for table in ("agent_jobs", "agent_threads", "agent_job_events", "agent_attempts"):
        assert migration["target"](f"SELECT 1 FROM {table} LIMIT 1") == [], (
            f"{table} must not be migrated"
        )


def test_the_target_is_migrated_as_part_of_the_import(migration, capsys) -> None:
    """`alembic upgrade head` runs here, not as a prerequisite the operator remembers.

    MIGRATION.md's deferred register makes this H1's job because the store's
    `initialize()` is a no-op — nothing else creates the two tables now.
    """
    assert mc.main(["--old-key-file", migration["key_file"], "--apply"]) == mc.EXIT_OK
    report = capsys.readouterr().out

    assert "target alembic revision:" in report
    assert "unknown" not in report and "unstamped" not in report
    assert sorted(row["id"] for row in migration["target"]("SELECT id FROM agent_repo_grants")) == [
        41,
        42,
    ]


def test_skip_migrations_leaves_the_schema_alone(migration, capsys) -> None:
    """The flag the runbook uses when the deploy already migrated.

    Against an empty target it fails loudly rather than silently importing
    nothing — which is the correct shape for a flag that says "someone else
    did this already".
    """
    exit_code = mc.main(["--old-key-file", migration["key_file"], "--apply", "--skip-migrations"])
    assert exit_code == mc.EXIT_CONFIG
    assert migration["target"]("SELECT tablename FROM pg_tables WHERE schemaname = 'public'") == []
