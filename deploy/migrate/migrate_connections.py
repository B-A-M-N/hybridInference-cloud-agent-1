#!/usr/bin/env python3
"""Move source-control connections from the gateway's database into this one.

Two tables, and only two: ``agent_repo_grants`` and
``agent_gitlab_connections``. Job, thread, attempt, event and artifact history
stays in the gateway's database and dies with it (decision DR5, recorded in
MIGRATION.md). A user loses their task history at cutover and keeps the thing
that would otherwise take a trip through GitHub and GitLab to rebuild.

**This is not a copy.** ``SourceControlCipher`` was re-keyed in E5: the gateway
derived its Fernet key from ``API_KEY_SECRET``, this service derives it from
``AGENT_SOURCE_CONTROL_ENCRYPTION_KEY``. The domain separator
(``hybridinference/source-control/v1\\0``) is unchanged, so the transform is
exactly decrypt-with-old / encrypt-with-new. Copying the ciphertext instead
produces a GitLab connection that lists in the UI, shows the right username,
and fails the first time anything asks it for a token — which is the worst of
the available failure modes, because it fails for the user rather than for the
operator running this.

**The old key material never becomes configuration here.** It is read from a
file (or stdin), used, and dropped. This script deliberately does not read
``API_KEY_SECRET`` from its own environment: AGENTS.md rule 4 says this service
never sees the gateway's secrets, and an environment variable is inherited,
dumped by ``docker inspect``, and shipped by most log collectors. A file the
operator deletes afterwards is the smallest surface that still works. Passing
it on the command line is not offered at all — argv is world-readable in
``ps``.

Safe by default: without ``--apply`` nothing is written anywhere, and the
source connection is opened read-only regardless. With ``--apply`` the *schema*
is written first — ``alembic upgrade head`` has to run before the target's
tables can be read — so a run that is then refused has changed the target's
schema and no rows, and the report says exactly that.

Idempotent: a second run inserts nothing and reports every row as unchanged.
Re-encryption is *not* deterministic — Fernet carries a random IV and a
timestamp — so equality is decided on plaintext, never on stored bytes. That
is also what makes the reported checksum comparable between the two databases:
it is computed over decrypted content, so a matching pair proves the re-key
round-tripped rather than proving two blobs are byte-identical.

Usage::

    AGENT_MIGRATION_SOURCE_URL=postgresql://…/gateway \\
    AGENT_DATABASE_URL=postgresql://…/cloud_agent \\
    AGENT_SOURCE_CONTROL_ENCRYPTION_KEY=… \\
    uv run python deploy/migrate/migrate_connections.py \\
        --old-key-file /run/secrets/gateway-api-key-secret --apply

See ``deploy/migrate/README.md`` for where it is meant to be run from, and
``docs/cutover.md`` for where it sits in the runbook.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cloud_agent.storage.dsn import sync_sqlalchemy_url

REPO = Path(__file__).resolve().parents[2]
if str(REPO / "backend") not in sys.path:
    sys.path.insert(0, str(REPO / "backend"))

from cloud_agent.source_control import (
    ENV_ENCRYPTION_KEY,
    SourceControlCipher,
    SourceControlError,
)

#: DSN of the gateway database to read. Env only, never a flag: a DSN carries a
#: password and argv is readable by every process on the host.
ENV_SOURCE_URL = "AGENT_MIGRATION_SOURCE_URL"

#: DSN of this service's database — the same variable the service itself reads,
#: so an operator cannot migrate into a database the app is not using.
ENV_TARGET_URL = "AGENT_DATABASE_URL"

#: Path to the file holding the gateway's old key material.
ENV_OLD_KEY_FILE = "AGENT_MIGRATION_OLD_KEY_FILE"

#: Gateway secrets this repository is forbidden to hold. Named here only so the
#: script can say "I am ignoring this" when one is present in its environment.
GATEWAY_SECRET_ENV_NAMES = ("API_KEY_SECRET", "JWT_SECRET_KEY")

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_CONFIG = 2


@dataclass(frozen=True)
class TableSpec:
    """One migrating table: what to read, what identifies a row, what to re-key."""

    name: str
    columns: tuple[str, ...]
    #: Columns whose stored value is ciphertext under the source-control cipher.
    ciphertext_columns: tuple[str, ...]
    #: A unique constraint other than the primary key. A target row holding one
    #: of these under a *different* id is a collision the insert would hit, so
    #: it is detected and reported instead of raised.
    unique_key: tuple[str, ...]


REPO_GRANTS = TableSpec(
    name="agent_repo_grants",
    columns=("id", "user_id", "installation_id", "account_login", "created_at"),
    ciphertext_columns=(),
    unique_key=("user_id", "installation_id"),
)

GITLAB_CONNECTIONS = TableSpec(
    name="agent_gitlab_connections",
    columns=(
        "id",
        "user_id",
        "external_user_id",
        "username",
        "display_name",
        "web_url",
        "access_token_ciphertext",
        "refresh_token_ciphertext",
        "expires_at",
        "created_at",
        "updated_at",
    ),
    ciphertext_columns=("access_token_ciphertext", "refresh_token_ciphertext"),
    unique_key=("user_id",),
)

#: The whole migration. Order matters only for readability — the two tables are
#: independent and neither references the other.
TABLES: tuple[TableSpec, ...] = (REPO_GRANTS, GITLAB_CONNECTIONS)


class MigrationError(Exception):
    """A condition that must stop the migration before anything is written."""


class MissingTableError(MigrationError):
    """A table the migration needs does not exist in the database it read."""


# ── Content, plaintext-normalised ──────────────────────────────────────


def _render(value: Any) -> str:
    """Render one column value for fingerprinting, stably across databases."""
    if value is None:
        # A sentinel no rendered value can collide with: without it, NULL and
        # the empty string fingerprint identically.
        return "\x00"
    if isinstance(value, datetime):
        moment = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        return moment.astimezone(timezone.utc).isoformat()
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def row_fingerprint(spec: TableSpec, row: dict[str, Any], plaintext: dict[str, str]) -> bytes:
    """Digest one row with its ciphertext columns replaced by their plaintext.

    Args:
        spec: The table this row belongs to.
        row: Column values as read from the database.
        plaintext: Decrypted values, keyed by ciphertext column name.

    Returns:
        A digest that is equal for the same logical row on both sides of the
        migration, despite the ciphertext differing by construction.
    """
    parts = []
    for column in spec.columns:
        value = plaintext[column] if column in spec.ciphertext_columns else row[column]
        parts.append(f"{column}={_render(value)}")
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).digest()


def checksum(spec: TableSpec, fingerprints: list[bytes]) -> str:
    """Combine row fingerprints into one order-independent table checksum.

    Sorted, so the two databases need not return rows in the same order, and
    domain-separated by table name so an empty table's checksum is not shared
    with every other empty table.
    """
    digest = hashlib.sha256(b"freeinference-cloud-agent/migrate/v1\0" + spec.name.encode("utf-8"))
    for fingerprint in sorted(fingerprints):
        digest.update(fingerprint)
    return digest.hexdigest()


def decrypt_rows(
    spec: TableSpec, rows: list[dict[str, Any]], cipher: SourceControlCipher | None
) -> list[dict[str, str]]:
    """Decrypt every ciphertext column, failing closed on the whole table.

    One undecryptable row means the key is wrong, or that row was written by a
    third key. Either way the answer is to stop before writing anything, not to
    migrate the rows that happened to work: a partial import looks like a
    successful one to everybody who is not reading the report.

    Args:
        spec: The table being read.
        rows: Rows as read from the database.
        cipher: The cipher those rows were written under. May be ``None`` when
            the table has no ciphertext columns.

    Returns:
        One plaintext mapping per row, aligned with ``rows``.

    Raises:
        MigrationError: If any row fails to decrypt.
    """
    if not spec.ciphertext_columns:
        return [{} for _ in rows]
    if cipher is None:
        raise MigrationError(f"{spec.name} has encrypted columns but no cipher was supplied")
    plaintexts: list[dict[str, str]] = []
    failed: list[Any] = []
    for row in rows:
        decoded: dict[str, str] = {}
        for column in spec.ciphertext_columns:
            try:
                decoded[column] = cipher.decrypt(row[column])
            except SourceControlError:
                failed.append(row["id"])
                break
        plaintexts.append(decoded)
    if failed:
        raise MigrationError(
            f"{len(failed)} of {len(rows)} {spec.name} rows did not decrypt "
            f"(ids {sorted(failed)[:10]}). The key material is wrong for these rows, "
            f"or they were written under a third key. Nothing has been written."
        )
    return plaintexts


# ── Planning ───────────────────────────────────────────────────────────


@dataclass
class TablePlan:
    """What this run would do to one table, decided before anything is written."""

    spec: TableSpec
    insert: list[dict[str, Any]] = field(default_factory=list)
    refresh: list[dict[str, Any]] = field(default_factory=list)
    unchanged: list[dict[str, Any]] = field(default_factory=list)
    conflict: list[dict[str, Any]] = field(default_factory=list)
    #: How many rows the target holds, counted rather than derived. It is not
    #: ``refresh + unchanged``: a target row the source has no counterpart for
    #: — a connection made on the new stack during a rehearsal — is in the
    #: table, is in the target checksum, and so has to be in the count the
    #: checksum is read next to.
    target_total: int = 0
    source_checksum: str = ""
    target_checksum: str = ""

    @property
    def blocked(self) -> bool:
        """Whether this plan cannot be applied without losing or colliding data."""
        return bool(self.conflict)


def plan_table(
    spec: TableSpec,
    source_rows: list[dict[str, Any]],
    source_plaintext: list[dict[str, str]],
    target_rows: list[dict[str, Any]],
    target_plaintext: list[dict[str, str]],
) -> TablePlan:
    """Classify every source row against the target's current contents.

    Four outcomes, and the two that are not "insert" or "unchanged" are the
    reason this is a planning step rather than an ``INSERT … ON CONFLICT DO
    NOTHING``:

    - **refresh** — the id is present, names the *same* owner, and its content
      differs. An id that names a different owner is a **conflict**, not a
      refresh: both databases assign ids of their own, so a collision is
      ordinary, and refreshing across it would overwrite somebody's connection
      with a stranger's. Silently
      skipping it is how a rehearsal import becomes a stale credential nobody
      notices until it fails.
    - **conflict** — the id is absent but another row already holds the same
      unique key. Inserting would raise; reporting says which user to look at.
    """
    plan = TablePlan(spec=spec, target_total=len(target_rows))
    by_id = {
        row["id"]: (row, text) for row, text in zip(target_rows, target_plaintext, strict=True)
    }
    by_unique = {tuple(row[column] for column in spec.unique_key): row["id"] for row in target_rows}

    for row, text in zip(source_rows, source_plaintext, strict=True):
        existing = by_id.get(row["id"])
        if existing is None:
            unique = tuple(row[column] for column in spec.unique_key)
            holder = by_unique.get(unique)
            if holder is not None:
                plan.conflict.append({**row, "_target_id": holder})
            else:
                plan.insert.append(row)
            continue
        target_row, target_text = existing
        source_key = tuple(row[column] for column in spec.unique_key)
        target_key = tuple(target_row[column] for column in spec.unique_key)
        if source_key != target_key:
            # **Same id, different owner.** Both databases assign ids of their
            # own, so a numeric collision is ordinary — and treating it as a
            # refresh would overwrite one user's source-control connection with
            # another's. A refusal costs a manual decision; the alternative
            # costs somebody their repository access, silently, with the
            # migration reporting success.
            plan.conflict.append({**row, "_target_id": target_row["id"]})
            continue
        if row_fingerprint(spec, row, text) == row_fingerprint(spec, target_row, target_text):
            plan.unchanged.append(row)
        else:
            plan.refresh.append(row)

    plan.source_checksum = checksum(
        spec,
        [
            row_fingerprint(spec, row, text)
            for row, text in zip(source_rows, source_plaintext, strict=True)
        ],
    )
    plan.target_checksum = checksum(
        spec,
        [
            row_fingerprint(spec, row, text)
            for row, text in zip(target_rows, target_plaintext, strict=True)
        ],
    )
    return plan


# ── Database access ────────────────────────────────────────────────────


def sqlalchemy_url(dsn: str) -> str:
    """Rewrite an asyncpg DSN into the driver Alembic actually has.

    Re-exported from :mod:`cloud_agent.storage.dsn` rather than restated:
    `migrations/env.py` needs the identical rule, and two copies of a
    driver-rewriting table drift into a script that migrates one database and
    an Alembic run that cannot open it.
    """
    return sync_sqlalchemy_url(dsn)


def upgrade_target(target_url: str) -> None:
    """Run ``alembic upgrade head`` against the database being imported into.

    Required, not optional: the two tables must exist before rows can land in
    them, and the baseline is where they are created now that the store's
    ``initialize()`` is a no-op.

    Run *before* the import for that reason. That ordering is only safe while
    the baseline is the only revision — a future revision that transforms data
    would have to run again afterwards, or the imported rows would miss it.

    It also runs before the conflict and staleness checks, because those read
    tables that may not exist yet. So a ``--apply`` run that is then refused
    has still upgraded the target's schema, and the report says so rather than
    claiming nothing was written.
    """
    from alembic import command
    from alembic.config import Config

    config = Config(str(REPO / "alembic.ini"))
    config.set_main_option("script_location", str(REPO / "migrations"))
    url = sqlalchemy_url(target_url)
    config.set_main_option("sqlalchemy.url", url)
    # migrations/env.py prefers the environment over the config, so setting the
    # option alone would silently migrate whatever the ambient DSN points at.
    previous = os.environ.get(ENV_TARGET_URL)
    os.environ[ENV_TARGET_URL] = url
    try:
        command.upgrade(config, "head")
    finally:
        if previous is None:
            os.environ.pop(ENV_TARGET_URL, None)
        else:
            os.environ[ENV_TARGET_URL] = previous


async def read_table(conn: Any, spec: TableSpec) -> list[dict[str, Any]]:
    """Read one table whole, ordered by id.

    Raises:
        MissingTableError: If the table does not exist. A target that has never
            been migrated is the *normal* state for a rehearsal, so this is a
            condition to report rather than a traceback.
    """
    import asyncpg

    columns = ", ".join(spec.columns)
    try:
        rows = await conn.fetch(f"SELECT {columns} FROM {spec.name} ORDER BY id")
    except asyncpg.exceptions.UndefinedTableError as exc:
        raise MissingTableError(spec.name) from exc
    return [dict(row) for row in rows]


async def insert_rows(
    conn: Any, spec: TableSpec, rows: list[dict[str, Any]], cipher: SourceControlCipher | None
) -> int:
    """Insert rows with their source ids preserved, re-encrypting as they go."""
    if not rows:
        return 0
    columns = ", ".join(spec.columns)
    placeholders = ", ".join(f"${index}" for index in range(1, len(spec.columns) + 1))
    statement = f"INSERT INTO {spec.name} ({columns}) VALUES ({placeholders})"
    for row in rows:
        await conn.execute(statement, *_values_for_write(spec, row, cipher))
    return len(rows)


async def refresh_rows(
    conn: Any, spec: TableSpec, rows: list[dict[str, Any]], cipher: SourceControlCipher | None
) -> int:
    """Overwrite existing rows, matched on the preserved id."""
    if not rows:
        return 0
    updatable = [column for column in spec.columns if column != "id"]
    assignments = ", ".join(
        f"{column} = ${index}" for index, column in enumerate(updatable, start=2)
    )
    statement = f"UPDATE {spec.name} SET {assignments} WHERE id = $1"
    for row in rows:
        values = _values_for_write(spec, row, cipher)
        by_column = dict(zip(spec.columns, values, strict=True))
        await conn.execute(statement, by_column["id"], *(by_column[c] for c in updatable))
    return len(rows)


def _values_for_write(
    spec: TableSpec, row: dict[str, Any], cipher: SourceControlCipher | None
) -> list[Any]:
    """Return the row's values, ciphertext columns re-encrypted under the new key.

    The plaintext exists only inside this call. It is never returned, logged,
    or fingerprinted from here — fingerprints are taken during planning, from
    values that never leave the process either.
    """
    values: list[Any] = []
    for column in spec.columns:
        if column in spec.ciphertext_columns:
            if cipher is None:
                raise MigrationError(f"{spec.name}.{column} needs a cipher to be written")
            values.append(cipher.encrypt(row[f"_plain_{column}"]))
        else:
            values.append(row[column])
    return values


async def resync_sequence(conn: Any, spec: TableSpec) -> None:
    """Advance the id sequence past the ids this import preserved.

    Inserting an explicit value into a ``BIGSERIAL`` column does not move its
    sequence. Skip this and the *first* connection a user makes after cutover
    collides with the id of a migrated one — a duplicate-key error in a flow
    nobody re-tests after the migration reported success.
    """
    await conn.execute(
        f"""
        SELECT setval(
            pg_get_serial_sequence('{spec.name}', 'id'),
            GREATEST(COALESCE((SELECT MAX(id) FROM {spec.name}), 0), 1),
            (SELECT COUNT(*) > 0 FROM {spec.name})
        )
        """
    )


async def target_revision(conn: Any) -> str:
    """Report the Alembic revision the target is stamped at."""
    try:
        value = await conn.fetchval("SELECT version_num FROM alembic_version")
    except Exception:
        return "unknown (no alembic_version table)"
    return str(value) if value else "unstamped"


# ── Key material ───────────────────────────────────────────────────────


def read_key_material(path: str) -> str:
    """Read key material from a file, or from stdin when ``path`` is ``-``.

    A single trailing newline is stripped, because every editor and every
    ``echo`` adds one and a key that differs by one byte derives a different
    Fernet key with no diagnostic beyond "nothing decrypts".
    """
    try:
        raw = sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise MigrationError(f"could not read key material from {path!r}: {exc}") from exc
    material = raw[:-1] if raw.endswith("\n") else raw
    if not material.strip():
        raise MigrationError(f"key material at {path!r} is empty")
    return material


# ── Reporting ──────────────────────────────────────────────────────────


def render_report(
    plans: list[TablePlan],
    *,
    applied: bool,
    revision: str,
    missing: tuple[str, ...] = (),
    refused: bool = False,
    schema_upgraded: bool = False,
) -> str:
    """Render the row counts and checksums this run is judged on.

    Args:
        plans: One plan per migrating table.
        applied: Whether rows were actually written.
        revision: The Alembic revision the target is stamped at.
        missing: Tables absent from the target (dry runs only).
        refused: Whether ``--apply`` was asked for and then refused.
        schema_upgraded: Whether ``alembic upgrade head`` ran against the
            target during *this* invocation. It runs before the refusal checks
            — the target's tables must exist to be read — so a refused run has
            still changed the target's schema, and a report saying "nothing was
            written" would be false about the one thing an operator would then
            have to undo.
    """
    lines = [
        "",
        f"{'table':<26}{'source':>8}{'target':>8}{'insert':>8}"
        f"{'refresh':>9}{'same':>7}{'conflict':>10}",
    ]
    for plan in plans:
        source_count = (
            len(plan.insert) + len(plan.refresh) + len(plan.unchanged) + len(plan.conflict)
        )
        lines.append(
            f"{plan.spec.name:<26}{source_count:>8}{plan.target_total:>8}{len(plan.insert):>8}"
            f"{len(plan.refresh):>9}{len(plan.unchanged):>7}{len(plan.conflict):>10}"
        )
    lines.append("")
    for plan in plans:
        verdict = "MATCH" if plan.source_checksum == plan.target_checksum else "DIFFERS"
        lines.append(f"checksum {plan.spec.name}")
        lines.append(f"  source {plan.source_checksum}")
        lines.append(f"  target {plan.target_checksum}  {verdict}")
    lines.append("")
    lines.append(f"target alembic revision: {revision}")
    if applied:
        lines.append("mode: applied")
    elif refused:
        lines.append("mode: refused — no rows were written")
        if schema_upgraded:
            # The upgrade runs before the target can be read, so by the time a
            # conflict or a stale row is found the schema has already moved.
            # Saying "nothing was written" here would send an operator away
            # believing the target is untouched.
            lines.append(
                "note: the schema WAS written — 'alembic upgrade head' ran against the "
                f"target before the refusal and left it at {revision}. No rows were "
                "inserted, updated or deleted."
            )
    else:
        lines.append("mode: dry run — nothing was written")
    for table in missing:
        lines.append(
            f"note: {table} does not exist in the target yet; "
            "'alembic upgrade head' creates it when this is run with --apply"
        )
    for plan in plans:
        for row in plan.conflict:
            lines.append(
                f"CONFLICT {plan.spec.name}: source id {row['id']} carries a unique key "
                f"already held by target id {row['_target_id']}"
            )
    return "\n".join(lines)


# ── Entry point ────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI parser.

    Separate from :func:`main` so a runbook step can be checked against the
    flags that exist rather than against the flags someone remembered.
    """
    parser = argparse.ArgumentParser(
        description=(
            "Migrate agent_repo_grants and agent_gitlab_connections from the "
            "gateway database into this service's database, re-keying the "
            "stored credentials."
        )
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually write. Without it the script reads both databases and reports only.",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help=(
            "Overwrite target rows whose content diverged from the source "
            "(a rehearsal import, then a user reconnecting on the old stack)."
        ),
    )
    parser.add_argument(
        "--skip-migrations",
        action="store_true",
        help="Do not run 'alembic upgrade head' first. Only when the deploy already did.",
    )
    parser.add_argument(
        "--old-key-file",
        default=os.environ.get(ENV_OLD_KEY_FILE),
        help=(
            "File holding the gateway's old key material, or '-' for stdin. "
            f"Defaults to ${ENV_OLD_KEY_FILE}. Never passed as a value: argv is "
            "readable by every process on the host."
        ),
    )
    parser.add_argument(
        "--new-key-file",
        default=None,
        help=f"File holding this service's key material. Defaults to ${ENV_ENCRYPTION_KEY}.",
    )
    return parser


async def run(args: argparse.Namespace) -> int:
    """Read, plan, optionally write, then verify. Returns the process exit code."""
    import asyncpg

    # Said first, before any configuration check can end the run. An operator
    # who set API_KEY_SECRET expecting it to be *used* is exactly the operator
    # who then omits --old-key-file — so emitting this after that check meant
    # the notice was unreachable in the one case it exists for.
    present = [name for name in GATEWAY_SECRET_ENV_NAMES if os.environ.get(name)]
    if present:
        print(
            f"note: {', '.join(present)} is set in this environment and is being ignored. "
            "This service never reads a gateway secret from its environment; pass the old "
            "key with --old-key-file instead.",
            file=sys.stderr,
        )

    source_url = os.environ.get(ENV_SOURCE_URL, "")
    target_url = os.environ.get(ENV_TARGET_URL, "")
    if not source_url or not target_url:
        raise MigrationError(f"{ENV_SOURCE_URL} and {ENV_TARGET_URL} must both be set")
    if source_url == target_url:
        raise MigrationError(
            f"{ENV_SOURCE_URL} and {ENV_TARGET_URL} name the same database; "
            "this migration reads one and writes the other"
        )
    if not args.old_key_file:
        raise MigrationError(
            "--old-key-file (or $" + ENV_OLD_KEY_FILE + ") is required: the stored "
            "credentials were written under the gateway's key and must be decrypted "
            "with it before they can be re-encrypted under this service's key"
        )

    old_cipher = SourceControlCipher(read_key_material(args.old_key_file))
    new_cipher = (
        SourceControlCipher(read_key_material(args.new_key_file))
        if args.new_key_file
        else SourceControlCipher()
    )

    source = await asyncpg.connect(source_url)
    try:
        # Belt and braces: this script has no UPDATE against the source, and
        # the session refuses one anyway.
        await source.execute("SET default_transaction_read_only = on")
        # **One snapshot for every table.** asyncpg commits each statement on
        # its own, so reading two tables in sequence reads them at two
        # different moments — and a user who connects GitHub between them
        # lands in one export and not the other. The cutover window is exactly
        # when that is most likely, because the old stack is still serving.
        #
        # `REPEATABLE READ` rather than a lock: this must not block the
        # gateway it is reading from, and a consistent view is all that is
        # wanted. Postgres gives every statement in the transaction the same
        # snapshot, which is the guarantee — not an ordering, a *moment*.
        async with source.transaction(isolation="repeatable_read", readonly=True):
            source_data = {spec.name: await read_table(source, spec) for spec in TABLES}
    finally:
        await source.close()

    source_plain = {
        spec.name: decrypt_rows(spec, source_data[spec.name], old_cipher) for spec in TABLES
    }
    # Carry the plaintext on the row so the write path re-encrypts the value
    # this run actually verified, rather than decrypting a second time.
    for spec in TABLES:
        for row, text in zip(source_data[spec.name], source_plain[spec.name], strict=True):
            for column, value in text.items():
                row[f"_plain_{column}"] = value

    schema_upgraded = False
    if args.apply and not args.skip_migrations:
        upgrade_target(target_url)
        # Recorded because the report has to be able to say so: everything
        # below can still refuse, and this has already changed the target.
        schema_upgraded = True

    target = await asyncpg.connect(target_url)
    try:
        plans = []
        missing: list[str] = []
        for spec in TABLES:
            try:
                target_rows = await read_table(target, spec)
            except MissingTableError:
                # A target that has never been migrated is the normal state for
                # a rehearsal, and reads as "empty". Under --apply it is a real
                # error, because the upgrade above was supposed to create it.
                if args.apply:
                    raise MigrationError(
                        f"{spec.name} does not exist in the target database. Run without "
                        "--skip-migrations, or apply 'alembic upgrade head' to it first."
                    ) from None
                target_rows = []
                missing.append(spec.name)
            plans.append(
                plan_table(
                    spec,
                    source_data[spec.name],
                    source_plain[spec.name],
                    target_rows,
                    decrypt_rows(spec, target_rows, new_cipher),
                )
            )

        blocked = [plan for plan in plans if plan.blocked]
        stale = [plan for plan in plans if plan.refresh and not args.refresh]

        if args.apply and not blocked and not stale:
            # **The plan is recomputed inside the transaction that writes it.**
            # The one above was read without a lock and is a prediction; acting
            # on it means writing against a database that may have moved. The
            # case that matters is not a primary-key clash — that aborts loudly
            # — but the same user connecting on the *new* stack in between:
            # they get a fresh id, this inserts the old one, and the user ends
            # up with two connection rows that no conflict check ever saw,
            # because the check ran against a snapshot taken before theirs
            # existed.
            #
            # `EXCLUSIVE` blocks writers and allows readers, which is the right
            # trade for a cutover: the new stack should not be accepting
            # connections yet, and if it is, waiting is the correct behaviour.
            async with target.transaction():
                for spec in TABLES:
                    await target.execute(f"LOCK TABLE {spec.name} IN EXCLUSIVE MODE")

                plans = []
                for spec in TABLES:
                    target_rows = await read_table(target, spec)
                    plans.append(
                        plan_table(
                            spec,
                            source_data[spec.name],
                            source_plain[spec.name],
                            target_rows,
                            decrypt_rows(spec, target_rows, new_cipher),
                        )
                    )

                # Re-checked under the lock. A conflict that appeared between
                # the preview and now must stop the write, not be carried
                # through it — and rolling back is the whole reason this is
                # decided inside the transaction rather than before it.
                blocked = [plan for plan in plans if plan.blocked]
                stale = [plan for plan in plans if plan.refresh and not args.refresh]
                if blocked or stale:
                    raise MigrationError(
                        "the target changed between the preview and the write: "
                        + ", ".join(
                            sorted(
                                {plan.spec.name for plan in blocked}
                                | {plan.spec.name for plan in stale}
                            )
                        )
                        + ". Nothing was written; re-run to see the new plan."
                    )

                for plan in plans:
                    await insert_rows(target, plan.spec, plan.insert, new_cipher)
                    if args.refresh:
                        await refresh_rows(target, plan.spec, plan.refresh, new_cipher)
                    await resync_sequence(target, plan.spec)

            # Re-read and re-plan, so the reported checksums describe the
            # database as it now stands rather than as it was predicted to be.
            plans = []
            for spec in TABLES:
                target_rows = await read_table(target, spec)
                plans.append(
                    plan_table(
                        spec,
                        source_data[spec.name],
                        source_plain[spec.name],
                        target_rows,
                        decrypt_rows(spec, target_rows, new_cipher),
                    )
                )
        revision = await target_revision(target)
    finally:
        await target.close()

    print(
        render_report(
            plans,
            applied=bool(args.apply and not blocked and not stale),
            revision=revision,
            missing=tuple(missing),
            refused=bool(args.apply and (blocked or stale)),
            schema_upgraded=schema_upgraded,
        )
    )

    if blocked:
        print(
            "\nrefused: a target row already holds a migrating row's unique key under a "
            "different id. Resolve by hand — the two rows are the same user's connection "
            "made twice.",
            file=sys.stderr,
        )
        return EXIT_FAILED
    if stale:
        print(
            "\nrefused: target rows diverge from the source. Re-run with --refresh to "
            "overwrite them, having checked that the source is the newer of the two.",
            file=sys.stderr,
        )
        return EXIT_FAILED
    if args.apply and any(plan.source_checksum != plan.target_checksum for plan in plans):
        print(
            "\nfailed: the target does not match the source after import. The rows are "
            "written; do not proceed with the cutover until this is understood.",
            file=sys.stderr,
        )
        return EXIT_FAILED
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    args = build_parser().parse_args(argv)
    try:
        return asyncio.run(run(args))
    except (MigrationError, SourceControlError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_CONFIG


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
