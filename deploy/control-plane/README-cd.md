# Continuous deployment

Two branches, two environments, and they federate with the matching gateway:

| Branch | Environment | Deploys | Gateway it talks to |
|---|---|---|---|
| `dev` | staging | automatically, on green CI | the gateway's own `dev`, i.e. its staging deployment |
| `main` | production | only when dispatched | the gateway's `main`, i.e. its production deployment |

`main` is not a branch anyone commits to: `sync-main.yml` fast-forwards it to
`dev` on request, and that is the only way it moves.

## Staging

Merging to `dev` deploys. `.github/workflows/deploy-staging.yml` waits for
`CI` to conclude green on `dev`, then runs a deploy script on each target.

**Two machines, two jobs, neither waiting on the other.** The control plane and
web app run on spark2 (ARM64); the runners, workspace broker, egress proxy and
tunnels run on the runner host (x86_64). They are deployed by
`deploy/control-plane/deploy.sh` and `deploy/host/deploy.sh` respectively, each
by a runner installed on the machine it deploys. A control plane that fails to
come up is not a reason to leave the runner host on an older commit, nor the
reverse.

The host script is thin on purpose: `agent_remote_runner.sh up` already checks
the daemon, requires the environment, runs the isolation preflight **fail
closed**, builds the sandbox image, starts the stack, and probes the gateway
from inside the closed network. Re-implementing any of that in the deploy would
give it a second opinion about whether the host may run untrusted code.

## One stack per host

`deploy/host/deploy.sh` refuses to start when containers from a *different*
compose project are already running this stack. That is not a hypothetical:
this host was first brought up by hand as project `agenthost` while the script
uses `hybridinference-agent-remote`, and deploying produced two complete stacks
— six runners claiming instead of three, and **two workspace brokers**.

The brokers are the dangerous half. The control plane stores one `broker_url`
per host, so whichever stack heartbeats last wins it, and a job claimed by a
runner in one stack has its files and terminal served from the other. The
symptom is "the workspace is empty", which is nobody's idea of "there are two
of me". `ALLOW_STRAY_STACK=1` overrides.

## Why there is no SSH in any of this

The runners are installed on spark2, which is also the deployment target, and
they run as the user that owns `/srv/freeinference-cloud-agent`. So the deploy
job runs *on* the host: no deploy key, no `known_hosts` secret, no `sudo`. The
gateway's equivalent workflow carries about sixty lines of host-key handling
for one reason — its runners are on a different machine from its target.

For the same reason the host holds **no git credential**. `deploy.sh` fetches
the commit from the runner's own workspace, which is a full checkout of exactly
that commit, on the same disk, authenticated by the job's token. A deploy key
on the box would be a standing secret whose only job is to re-download
something already sitting a few directories away.

## One-time setup on a new host

`deploy.sh` refuses to run against a directory that is not a git checkout,
because `git reset --hard` in a plain directory silently does nothing useful.
Converting an rsync-populated tree is safe **only after** proving it matches
the commit it claims to be:

```bash
cd /srv/freeinference-cloud-agent
git init -b dev
git remote add origin https://github.com/HarvardMadSys/freeinference-cloud-agent.git
git fetch <a local clone, or origin with credentials>
git reset --mixed "$(cat .deployed-sha)"   # HEAD + index only; files untouched

# This must print nothing. If it prints anything, the host has changes that
# exist nowhere in git and the next deploy would erase them — get them into a
# PR first. That is not hypothetical: #29 recovered two real fixes this way.
git status --porcelain --untracked-files=no
```

`.env` is untracked and stays untracked — it holds this deployment's secrets.
`git reset --hard` never touches untracked files, so deploys leave it alone.
Never run `git clean` in this tree.

## Rolling back

Dispatch **Deploy Staging** with `sha` set to the commit to go back to.
Rollback is a deploy of an older commit; there is deliberately no separate
mechanism, so the emergency path is the one that runs on every merge.

A rollback across a migration is not automatic. Alembic runs forward on every
deploy; going back to a commit whose schema is older needs the downgrade run by
hand first. Check whether `migrations/` changed between the two commits before
assuming a rollback is just a redeploy.

## What the script refuses to do

- Deploy to a directory that is not a git checkout.
- Deploy over modified tracked files (`ALLOW_DIRTY=1` overrides, and discards).
- Deploy a commit other than the one asked for — it verifies the source clone's
  HEAD matches the requested SHA, and verifies the result after resetting.
- Write `.deployed-sha` before the health checks pass. That file answers "what
  is actually running here", and writing it early answers it wrongly for every
  failed deploy.

## Production — what is still missing

`.github/workflows/deploy-production.yml` exists and runs nothing yet, by
construction: its jobs ask for runner labels no machine carries, so a dispatch
queues instead of deploying. Borrowing staging's labels in the meantime would
have made the first production dispatch redeploy staging.

To finish it:

1. **Two production hosts**, as on staging — one for the control plane and web
   app, one for the runners. Install a repository runner on each, labelled
   `deploy-cloud-agent-prod` and `deploy-agent-host-prod` respectively.
2. **Convert each deploy root to a git checkout** and place its `.env`, exactly
   as in *One-time setup on a new host* above.
3. **Set the repository variables** the workflow refuses to run without:
   `AGENT_PROD_DEPLOY_ROOT`, `AGENT_PROD_HOST_DEPLOY_ROOT`, and
   `AGENT_PROD_COMPOSE_PROJECT` only if that machine runs another stack.
4. **Configure the production gateway first** — `IDENTITY_JWT_PRIVATE_KEY`,
   `IDENTITY_ALLOWED_REDIRECTS`, and a `GATEWAY_GRANT_DISPATCH_TOKEN` that is
   *not* staging's — then confirm it from outside:

   ```bash
   GATEWAY_GRANT_DISPATCH_TOKEN=... python deploy/preflight/check_gateway.py https://<production gateway>
   ```

5. **Then** the gateway's own frontend can point `/agents` at this deployment
   (`AGENT_WEB_INTERNAL_URL` and `AGENT_CONTROL_PLANE_INTERNAL_URL`). That
   rewrite is the user-visible cutover, and it must come last: set before this
   service exists, it hands `/agents` to nothing.

Order matters throughout, and in one direction. This service calls the
gateway's identity and `/internal/*` routes; the gateway calls nothing here. A
control plane deployed ahead of the gateway that serves it is one calling
routes that answer 404.

Rollback is the same as staging's: dispatch **Deploy Production** with `sha`
set to the commit to go back to.

## Health checks

`/healthz` on the control plane, and the web app **at its base path** —
`AGENT_WEB_BASE_PATH`, not `/`. The app is mounted under a prefix, so `/` is a
404 by design; a check pointed there fails every deploy of a perfectly healthy
app. On staging: `/` → 404, `/agents` → 200, `/agents/` → 308.
