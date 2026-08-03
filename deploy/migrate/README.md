# deploy/migrate

One script, and the decision it forces.

`migrate_connections.py` moves `agent_repo_grants` and `agent_gitlab_connections`
out of the gateway's database and into this service's. Nothing else moves —
jobs, threads, attempts, events and artifacts stay behind and are lost at
cutover (decision DR5). The runbook step that calls this is
[docs/cutover.md](../../docs/cutover.md) step 4.

## Why it is not a copy

E5 re-keyed `SourceControlCipher`. The gateway derived its Fernet key from
`API_KEY_SECRET`; this service derives it from
`AGENT_SOURCE_CONTROL_ENCRYPTION_KEY`. The domain separator is unchanged, so
the transform is *decrypt with the old material, encrypt with the new one* —
and only the two `agent_gitlab_connections` ciphertext columns need it.
`agent_repo_grants` has no encrypted column at all; it is a plain id-preserving
copy.

Copy the ciphertext instead and you get a GitLab connection that lists in the
UI with the right username and fails the first time anything asks it for a
token. That failure lands on the user, days later, not on the operator running
the migration.

## Where it runs — decide before cutover day

The script must hold **both** keys at once for the length of one run. That is
unavoidable: nothing else can decrypt the old rows. It is also the one place
this repository brushes against AGENTS.md rule 4 ("this service never sees the
gateway's `API_KEY_SECRET`"), so the mechanics are deliberately narrow:

- the old key is read from a **file** (or stdin), never from the environment
  and never from `argv`;
- the script refuses to read `API_KEY_SECRET` even when it is present in its
  own environment, and says so on stderr;
- nothing is persisted: no cache, no state table, no log line carrying key
  material or plaintext.

What is **not** decided by the code is *which machine runs it*. Two workable
answers, and picking one is an operator decision that belongs in the deployment
overlay rather than here:

1. **On the gateway host**, with `AGENT_DATABASE_URL` pointing at the new
   database over the network. The old secret never leaves the machine that
   already holds it; the new key does travel there for the duration.
2. **On an operator workstation** with a route to both databases, both keys
   supplied as files under `/run` or a `tmpfs`, deleted afterwards.

Whichever is chosen, the agent host's persistent configuration must not end up
holding the gateway's secret — that is the property rule 4 protects. Record the
choice in the overlay and in the cutover ticket.

## Running it

```bash
export AGENT_MIGRATION_SOURCE_URL='postgresql://…/gateway_database'
export AGENT_DATABASE_URL='postgresql://…/cloud_agent'
export AGENT_SOURCE_CONTROL_ENCRYPTION_KEY='…'   # this service's key

# 1. Rehearse. Reads both databases, writes nothing, reports what it would do.
uv run python deploy/migrate/migrate_connections.py \
    --old-key-file /run/gateway-key

# 2. Apply. Runs `alembic upgrade head` against the target first.
uv run python deploy/migrate/migrate_connections.py \
    --old-key-file /run/gateway-key --apply

# 3. Prove it is idempotent — same command, `insert 0`, checksums still MATCH.
uv run python deploy/migrate/migrate_connections.py \
    --old-key-file /run/gateway-key --apply
```

| Flag | Use |
|---|---|
| *(none)* | Dry run. The default, deliberately. |
| `--apply` | Write. |
| `--refresh` | Overwrite target rows whose content diverged from the source. Needed when a rehearsal import is followed by a user reconnecting on the old stack. Without it, divergence is reported and the run refuses. |
| `--skip-migrations` | Do not run `alembic upgrade head` first. Only when the deploy already did. |
| `--old-key-file` | File with the gateway's key material, or `-` for stdin. Also `$AGENT_MIGRATION_OLD_KEY_FILE`. |
| `--new-key-file` | File with this service's key material. Defaults to `$AGENT_SOURCE_CONTROL_ENCRYPTION_KEY`. |

Exit codes: `0` fine, `1` the migration refused or verification failed, `2`
configuration is wrong.

## Reading the report

```text
table                       source  target  insert  refresh   same  conflict
agent_repo_grants               12       0      12        0      0         0
agent_gitlab_connections         3       0       3        0      0         0

checksum agent_repo_grants
  source 6f0f…
  target 6f0f…  MATCH
```

The checksum is computed over **decrypted** content, so `MATCH` is evidence the
re-key round-tripped — not merely that two blobs are byte-identical, which they
cannot be. Fernet ciphertext carries a random IV and a timestamp, so a byte
comparison would fail on a correct migration and is not attempted.

`conflict` is the column to read after a rehearsal: it counts source rows whose
`UNIQUE` key is already held in the target under a *different* id, which is the
same user having connected twice. The run refuses rather than raising a
duplicate-key error halfway through.

## What it does not do

- **No sequence surprise, but do check it.** The script resets each table's
  `BIGSERIAL` sequence past the highest imported id. Without that, the first
  connection a user makes after cutover collides with a migrated row's id.
- **No history.** By design. Do not add it here — DR5 is a decision, and the
  job tables reference each other and the runner pool in ways an id-preserving
  copy would not survive.
- **No dual-write, no reverse sync.** After cutover the gateway's copy is dead
  data. Rolling back means pointing the old stack back at it (see
  [docs/cutover.md](../../docs/cutover.md) § Rollback), not migrating rows
  backwards.
