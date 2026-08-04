# Single-host end-to-end smoke (task E11)

Bring up the control plane and one runner host on a single machine, point them
at a **staging** gateway, and drive one real job from an empty workspace to a
pull request on a scratch repository.

> **This runbook has not been executed.** It was written from the code and the
> deployment files, and E11's acceptance is a runbook *written from what was
> actually executed*. Until someone runs it and fills in the log at the bottom,
> treat every step as a claim to be checked rather than a route known to work
> — and **when a step is wrong, fix this file in the same sitting**. A runbook
> corrected a week later is a runbook nobody trusts.
>
> Two things in particular are unverified by anything: the control-plane image
> has never been built locally (no Docker daemon was available where it was
> written; CI's `docker-build` matrix builds it), and no step here has been run
> against a live gateway.

## What this proves, and what it does not

**Proves:** the two processes start, find each other, authenticate to a gateway
they do not share secrets with, and can carry one job from creation to a
published PR — including the parts that only exist after the split (identity
sign-in, an inference grant, a host enrolment).

**Does not prove:** anything about migrated data (that is H1 and the cutover
smoke in [cutover.md](cutover.md)), anything about concurrency or host loss
(G7), or that the gateway's side is deployed — at the time of writing the
gateway contracts live in unmerged pull requests, so **the very first thing to
check is that they are live on the staging gateway you are pointing at**.

## 0. Preconditions

- [ ] A machine with Docker, ≥8 GB RAM, and a checkout of this repository.
- [ ] **The staging gateway serves the contracts.** All four must answer:
      ```bash
      curl -sS -o /dev/null -w '%{http_code} %{url_effective}\n' \
        "$GATEWAY_BASE_URL/v1/identity/jwks"
      ```
      then, with the dispatch token, `/internal/mcp-registry`,
      `/internal/model-catalog?user_id=<a real user>` and
      `/internal/users/<id>/status`. A 404 on any of them means the gateway
      side has not landed; **stop here** — everything below will fail closed,
      correctly, and you will spend an hour proving that.
- [ ] A scratch GitHub repository this run may open pull requests against, and
      a token that can. Not a repository anyone depends on: the point of this
      exercise is that a machine writes to it.
- [ ] A staging gateway account you can sign in with.

## 1. Configure

Generate this deployment's own secrets. **They are its own** — none is derived
from anything the gateway holds, and if you find yourself copying a value out
of the gateway's environment, that is the split being undone:

```bash
for name in AGENT_SESSION_SECRET AGENT_CONTROL_TOKEN_SECRET AGENT_SOURCE_CONTROL_ENCRYPTION_KEY; do
  printf '%s=%s\n' "$name" "$(openssl rand -hex 32)"
done
```

The dispatch token is the one value shared with the gateway, and it must be the
same string on both sides and different per environment:

```bash
printf 'GATEWAY_GRANT_DISPATCH_TOKEN=%s\n' "$(openssl rand -hex 32)"
```

Write those plus the gateway URLs and the Postgres password into an env file.
[ENVIRONMENT.md](../ENVIRONMENT.md) is the full list; the compose file refuses
to render without the required ones, so an omission fails immediately rather
than at first use.

- [ ] `docker compose -f deploy/control-plane/docker-compose.yml --env-file <file> config`
      renders without error.

## 2. Start the control plane

```bash
docker compose -f deploy/control-plane/docker-compose.yml --env-file <file> up -d
```

- [ ] `migrate` exits 0. It runs `alembic upgrade head`; the service creates no
      tables itself, so a failure here means the API will not start — which is
      the intended behaviour, not a race to wait out.
- [ ] `curl -sS localhost:8000/healthz` answers.
- [ ] The log shows the reaper starting. Without it nothing recovers a lost
      attempt, and its absence is invisible until a job hangs.

## 3. Sign in — the identity flow end to end

This is the step that exercises C1–C4 and E7/E8 together, and the first place a
missing gateway contract shows up as a human-visible failure.

- [ ] Open the web UI, choose sign-in, and land on the **gateway's**
      `/authorize` page.
- [ ] Approve. You return to this service with a session.
- [ ] `GET /v1/session/me` reports your gateway user id and role.
- [ ] Nothing in the control plane's logs or database contains a gateway
      session token. It exchanged a code for a short-lived identity token,
      minted its own cookie, and kept neither.

## 4. Enrol the host

The runner does not authenticate with a shared password; it is issued a
credential bound to the name an admin chose for it.

- [ ] `POST /admin/agent/runner-hosts/enroll-token` with the host's name,
      using `ADMIN_TOKEN`.
- [ ] Start the host stack from
      `deploy/host/docker-compose.agent-remote-runner.yml` with that token.
- [ ] `GET /admin/agent/runner-hosts` shows the host `online` with a broker URL.
- [ ] Confirm the token cannot be reused under a different name — register a
      second machine under a typo of the first and watch it be refused
      **without spending the token**. This is the protection that stops a typo
      evicting a live host.

## 5. One job, all the way

- [ ] Connect the scratch repository under Integrations.
- [ ] Create a job against it with a small, checkable instruction ("add a
      CONTRIBUTING.md saying X").
- [ ] It reaches `queued`, then the host claims it.
- [ ] **A grant was minted for the attempt.** The control plane's log records
      it; the gateway's `agent_grants` row names the same external job and
      attempt. This is the moment the split is doing its job: the sandbox holds
      a capability the gateway issued, and no key of the gateway's.
- [ ] Events stream to the browser over SSE while it runs.
- [ ] The agent's model calls succeed — proving the grant's `allowed_models`
      clamp resolved to something the account can actually reach.
- [ ] It reaches `succeeded`, and **a pull request is open on the scratch
      repository**, authored by the configured git identity.
- [ ] Open a terminal on the finished workspace; files list.

## 6. The parts that only fail later

Cheap to check now, expensive to discover at cutover:

- [ ] **Renewal.** Leave a job running longer than the grant TTL (300 s by
      default). It must keep working. If it dies at ~5 minutes, the renewal
      loop is not running and every long job on this deployment is broken.
- [ ] **Revocation.** Cancel a job mid-run; the grant is revoked within a
      reaper pass rather than living to its TTL.
- [ ] **Fail-closed.** Point `GATEWAY_BASE_URL` at a port nothing listens on
      and restart. Sign-in must refuse and job creation must refuse. If either
      *succeeds*, something is failing open and that is a stop-everything
      finding, not a smoke-test nit.
- [ ] **The gateway holds no attempt state.** Stop the control plane with a job
      running. Nothing at the gateway ends the attempt; the grant simply
      expires. This is the design — the fence is here, not there.

## 7. Tear down

- [ ] `docker compose ... down -v` on both stacks.
- [ ] Delete the scratch repository's branches, or the repository.
- [ ] Revoke the dispatch token and the scratch repo token if this machine is
      not staying.

## Log

Fill this in **while running**, not afterwards. An entry written from memory is
the thing this file exists to replace.

| Step | Run at (UTC) | Result | Notes |
|---|---|---|---|
| 0. Preconditions | | | |
| 1. Configure | | | |
| 2. Control plane up | | | |
| 3. Sign in | | | |
| 4. Host enrolled | | | |
| 5. Job → PR | | | |
| 6. Renewal / revoke / fail-closed | | | |
| 7. Tear down | | | |

**Corrections made to this runbook during the run:**

_(none yet — it has not been run)_
