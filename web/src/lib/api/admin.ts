// Admin client for the runner host pool.
//
// Every endpoint here returns the **whole pool** rather than the row it acted
// on, and the callers below hand that snapshot straight to state. That is the
// backend's design (`_snapshot` in `api/admin_hosts.py`) and it matters for the
// UI: a drain changes `slots_used` on one host and `status` on another the
// moment a claim lands, so a page that patched one row locally would show a
// pool that never existed.
//
// Paths are `/admin/agent/runner-hosts...` — the router's own prefix. Under the
// path-based deployment the browser calls `/agents/api/...`, and `config.apiBase`
// carries that prefix; nothing here should spell it out.

import { config } from '@/config/env';

import { fetchWithAuth, jsonOrThrow } from './client';

const API_BASE = config.apiBase;
const HOSTS = '/admin/agent/runner-hosts';

/** Status values the control plane stores; see `backend/cloud_agent/host_pool.py`. */
export type AgentHostStatus = 'enrolling' | 'online' | 'draining' | 'disabled';

export interface AgentRunnerHost {
  host: string;
  active: boolean;
  last_worker_id: string | null;
  first_seen_at: string;
  last_seen_at: string;
  /**
   * Age of the last claim poll. A runner polls only *between* jobs, so a busy
   * machine can look quiet for as long as its longest job — which is why the UI
   * says "last polled" and never "offline".
   */
  seconds_since_seen: number;
  /**
   * Everything from here down arrived with the dynamic pool, and a host that
   * joined by polling has none of it. Optional in the same way the server
   * defaults it: the page an operator opens to fix a bad host must render that
   * host, not fail on it.
   */
  status: AgentHostStatus;
  /**
   * True when the host redeemed an enrollment token, which is the difference
   * between a name that is a scheduling label and a name that is an identity.
   */
  enrolled: boolean;
  labels: Record<string, unknown>;
  capabilities: string[];
  slots_total: number | null;
  slots_used: number;
  runner_version: string | null;
  broker_url: string | null;
  last_heartbeat_at: string | null;
}

export interface AgentRunnerHostPool {
  hosts: AgentRunnerHost[];
  /** Pinned host, or null when any runner may claim. */
  active_host: string | null;
}

export interface HostEnrollToken {
  enroll_token: string;
  host: string;
  expires_at: string;
}

export async function getAgentRunnerHosts(): Promise<AgentRunnerHostPool> {
  return jsonOrThrow<AgentRunnerHostPool>(await fetchWithAuth(API_BASE, HOSTS));
}

/** Pin new claims to one host, or pass null so any runner may claim. */
export async function setActiveAgentRunnerHost(host: string | null): Promise<AgentRunnerHostPool> {
  const resp = await fetchWithAuth(API_BASE, `${HOSTS}/active`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ host }),
  });
  return jsonOrThrow<AgentRunnerHostPool>(resp);
}

/**
 * Drop a decommissioned host from the list. Housekeeping only — a runner still
 * polling there re-adds itself within seconds.
 */
export async function forgetAgentRunnerHost(host: string): Promise<AgentRunnerHostPool> {
  const resp = await fetchWithAuth(API_BASE, `${HOSTS}/${encodeURIComponent(host)}`, {
    method: 'DELETE',
  });
  return jsonOrThrow<AgentRunnerHostPool>(resp);
}

/** Stop this host taking new work; let what it has finish. */
export async function drainAgentRunnerHost(host: string): Promise<AgentRunnerHostPool> {
  const resp = await fetchWithAuth(API_BASE, `${HOSTS}/${encodeURIComponent(host)}/drain`, {
    method: 'POST',
  });
  return jsonOrThrow<AgentRunnerHostPool>(resp);
}

/** Put a drained host back to work. */
export async function undrainAgentRunnerHost(host: string): Promise<AgentRunnerHostPool> {
  const resp = await fetchWithAuth(API_BASE, `${HOSTS}/${encodeURIComponent(host)}/undrain`, {
    method: 'POST',
  });
  return jsonOrThrow<AgentRunnerHostPool>(resp);
}

/**
 * Revoke this host's credential and take it out of the pool.
 *
 * `force` is refused-by-default for a reason worth repeating at the call site:
 * attempts still running on that host are **not** stopped — nothing on this
 * side can reach them — they are left to their leases, which expire and requeue
 * the work exactly once.
 */
export async function removeAgentRunnerHost(
  host: string,
  force = false,
): Promise<AgentRunnerHostPool> {
  const query = force ? '?force=true' : '';
  const resp = await fetchWithAuth(
    API_BASE,
    `${HOSTS}/${encodeURIComponent(host)}/remove${query}`,
    {
      method: 'POST',
    },
  );
  return jsonOrThrow<AgentRunnerHostPool>(resp);
}

/**
 * Mint a one-time token that lets one named machine join the pool.
 *
 * The token comes back **once**: only its hash is stored, so there is no call
 * that can show it again. Lose it and issue another.
 */
export async function issueHostEnrollToken(
  host: string,
  ttlSeconds?: number,
): Promise<HostEnrollToken> {
  const body: { host: string; ttl_seconds?: number } = { host };
  if (ttlSeconds !== undefined) body.ttl_seconds = ttlSeconds;
  const resp = await fetchWithAuth(API_BASE, `${HOSTS}/enroll-token`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  return jsonOrThrow<HostEnrollToken>(resp);
}
