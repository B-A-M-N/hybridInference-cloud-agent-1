#!/usr/bin/env bash
#
# Deploy the runner host — runners, workspace broker, egress proxy, tunnels.
#
# The other half of `deploy/control-plane/deploy.sh`. They are separate because
# they are separate machines: the control plane holds the queue and the web app,
# this one executes jobs, and each is deployed by a runner installed on itself.
#
# **Thin on purpose.** `agent_remote_runner.sh up` already is the bring-up: it
# checks the daemon, requires the environment, runs the isolation preflight
# fail-closed, builds the sandbox image, starts the stack, and then probes the
# gateway *from inside the closed network* — the only vantage point whose answer
# means anything. Re-implementing any of that here would give the deploy a
# second opinion about whether the host is fit to run untrusted code, and the
# two would drift.
#
# Usage:  deploy.sh <sha>
#   DEPLOY_ROOT   required — where the deployed checkout lives. Deliberately
#                 without a default: this path differs per machine (the host
#                 runs as a user without write access to /srv) and a wrong
#                 guess would deploy into a directory nobody is serving from.
#   SOURCE_REPO   a local clone holding <sha> (default: $GITHUB_WORKSPACE)
#   REPLICAS      runner count (default: whatever .env says, else the script's)
set -euo pipefail

SHA="${1:-${DEPLOY_SHA:-}}"
SOURCE_REPO="${SOURCE_REPO:-${GITHUB_WORKSPACE:-}}"
DEPLOY_ROOT="${DEPLOY_ROOT:-}"

log() { printf '[deploy-host] %s\n' "$*"; }
die() { printf '[deploy-host] ERROR: %s\n' "$*" >&2; exit 1; }

[ -n "${SHA}" ] || die "no commit given: deploy.sh <sha>"
[ -n "${DEPLOY_ROOT}" ] || die "DEPLOY_ROOT is not set (repository variable AGENT_HOST_DEPLOY_ROOT)"

cd "${DEPLOY_ROOT}" 2>/dev/null || die "${DEPLOY_ROOT} does not exist"

git rev-parse --git-dir >/dev/null 2>&1 \
  || die "${DEPLOY_ROOT} is not a git checkout — convert it once first (deploy/control-plane/README-cd.md)"

# Same guard, same reason as the control plane's: this tree was populated by
# rsync, and on 2026-08-06 it still held files from two branches that had been
# deleted from the remote without ever being proposed. One of them was the fix
# for a live sign-in failure. A deploy that silently overwrote them would have
# destroyed the only copy.
DIRTY="$(git status --porcelain --untracked-files=no)"
if [ -n "${DIRTY}" ]; then
  printf '[deploy-host] local modifications in %s:\n%s\n' "${DEPLOY_ROOT}" "${DIRTY}" >&2
  if [ "${ALLOW_DIRTY:-0}" != "1" ]; then
    die "refusing to overwrite them. Get them into git, or re-run with ALLOW_DIRTY=1 to discard."
  fi
  log "ALLOW_DIRTY=1 — discarding the above"
fi

[ -f .env ] || die ".env is missing from ${DEPLOY_ROOT} — it holds this host's credentials and is deliberately not in git"

# Fetched from a clone on this machine: the runner's workspace is already a
# checkout of exactly this commit, so the host needs no git credential of its
# own for a private repository.
[ -n "${SOURCE_REPO}" ] \
  || die "no SOURCE_REPO and no GITHUB_WORKSPACE — point SOURCE_REPO at a local clone holding ${SHA}"
SOURCE_HEAD="$(git -C "${SOURCE_REPO}" rev-parse HEAD)"
[ "${SOURCE_HEAD}" = "${SHA}" ] \
  || die "SOURCE_REPO is at ${SOURCE_HEAD}, not the requested ${SHA} — refusing to deploy something other than what was asked for"

PREVIOUS="$(git rev-parse HEAD 2>/dev/null || echo unknown)"
log "previous: ${PREVIOUS}"
log "target:   ${SHA}"

git fetch --quiet "${SOURCE_REPO}" HEAD
git reset --hard --quiet FETCH_HEAD
NOW="$(git rev-parse HEAD)"
[ "${NOW}" = "${SHA}" ] || die "checkout landed on ${NOW}, expected ${SHA}"
log "checkout now at ${NOW}"

# A second stack under a different compose project name is the failure this
# catches, and it is not hypothetical: this host was brought up by hand as
# project `agenthost`, while `agent_remote_runner.sh` uses
# `hybridinference-agent-remote`. Deploying produced *two* complete stacks —
# six runners claiming instead of three, and two workspace brokers.
#
# The brokers are the dangerous half. The control plane stores one `broker_url`
# per host, so whichever stack heartbeats last wins it, and a job claimed by a
# runner in one stack has its files and terminal served from the other. That
# reads as "the workspace is empty", not as "there are two of me".
EXPECTED_PROJECT=hybridinference-agent-remote
STRAY="$(docker ps --format '{{.Label "com.docker.compose.project"}} {{.Names}}' 2>/dev/null \
  | awk -v want="${EXPECTED_PROJECT}" '$1 != want && $2 ~ /agent-(runner|workspace-broker|gateway-tunnel|mcp-tunnel|control-plane-tunnel)/ {print "  " $2 "  (project: " $1 ")"}')"
if [ -n "${STRAY}" ]; then
  printf '[deploy-host] containers from another compose project are running this same stack:\n%s\n' "${STRAY}" >&2
  if [ "${ALLOW_STRAY_STACK:-0}" != "1" ]; then
    die "refusing to add a second stack beside them. Stop the other one first:
  docker compose -p <that project> -f deploy/host/docker-compose.agent-remote-runner.yml --env-file .env down
Or set ALLOW_STRAY_STACK=1 if you genuinely want both."
  fi
  log "ALLOW_STRAY_STACK=1 — starting anyway"
fi

# Everything that makes this a deployment rather than a `git pull` lives in
# there, including the refusal to start runners on a host that cannot isolate
# them. It exits non-zero on a failed gateway probe, which is what turns a
# broken tunnel into a red deploy instead of jobs that claim and then fail.
log "handing over to agent_remote_runner.sh up"
if [ -n "${REPLICAS:-}" ]; then
  bash deploy/host/agent_remote_runner.sh up "${REPLICAS}"
else
  bash deploy/host/agent_remote_runner.sh up
fi

printf '%s\n' "${SHA}" > .deployed-sha
log "deployed ${SHA} (was ${PREVIOUS})"
