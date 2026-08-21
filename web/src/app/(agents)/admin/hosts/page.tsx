'use client';

// The runner host pool — the whole of this service's operations surface.
//
// Runners pull work, so nothing here moves a job to a machine: every control on
// this page changes who may *claim*, and takes effect on the losing host's next
// poll. The wording follows that. "Pinned", not "assigned"; "last polled", not
// "online".
//
// It replaces the section that lived in the gateway's admin settings tab, which
// had the pin, the poll age and forget — a third of the pool's lifecycle,
// because the other two thirds (enroll, drain/remove) did not exist yet.

import { useCallback, useEffect, useRef, useState } from 'react';

import { useAuth } from '@/components/providers';
import { hasRole } from '@/components/providers/AuthProvider';
import type { AgentRunnerHost } from '@/lib/api/admin';
import {
  drainAgentRunnerHost,
  forgetAgentRunnerHost,
  getAgentRunnerHosts,
  issueHostEnrollToken,
  removeAgentRunnerHost,
  setActiveAgentRunnerHost,
  undrainAgentRunnerHost,
} from '@/lib/api/admin';
import { getErrorMessage } from '@/lib/utils/errors';

import { formatAge, formatSlots } from './format';

// A runner polls only between jobs, so silence can mean "busy for an hour" as
// easily as "gone". Carried over from the gateway's section along with the
// wording it justifies.
const RECENT_POLL_SECONDS = 300;

// Well under the staleness threshold, so a host that stops polling is reported
// as stale within a poll or two of actually becoming so.
const REFRESH_MS = 30_000;

const ENROLL_TTL_SECONDS = 900;

const STATUS_STYLES: Record<string, string> = {
  online:
    'bg-emerald-50 dark:bg-emerald-500/10 text-emerald-700 dark:text-emerald-400 ring-emerald-600/20 dark:ring-emerald-500/25',
  draining:
    'bg-amber-50 dark:bg-amber-500/10 text-amber-700 dark:text-amber-400 ring-amber-600/20 dark:ring-amber-500/25',
  enrolling:
    'bg-blue-50 dark:bg-blue-500/10 text-blue-700 dark:text-blue-400 ring-blue-600/20 dark:ring-blue-500/25',
  disabled:
    'bg-gray-100 dark:bg-white/5 text-gray-600 dark:text-gray-400 ring-gray-500/20 dark:ring-white/15',
};

function StatusBadge({ status }: { status: string }) {
  const style = STATUS_STYLES[status] ?? STATUS_STYLES.disabled;
  return (
    <span
      className={`inline-flex items-center rounded-full px-2 py-0.5 text-[11px] font-medium ring-1 ring-inset ${style}`}
    >
      {status}
    </span>
  );
}

export default function AdminHostsPage() {
  const { state } = useAuth();
  const [hosts, setHosts] = useState<AgentRunnerHost[]>([]);
  const [activeHost, setActiveHost] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [enrollHost, setEnrollHost] = useState('');
  const [issuedToken, setIssuedToken] = useState<{ host: string; token: string } | null>(null);

  const mounted = useRef(true);
  const isAdmin = hasRole(state.user?.role, 'admin');

  const load = useCallback(async (background = false) => {
    if (!background) setLoading(true);
    setError(null);
    try {
      const next = await getAgentRunnerHosts();
      if (!mounted.current) return;
      setHosts(next.hosts);
      setActiveHost(next.active_host);
    } catch (e) {
      if (!mounted.current) return;
      // A failed background poll leaves the last good pool on screen rather
      // than replacing it with an error: the numbers going stale is a smaller
      // lie than the page claiming there is nothing there.
      if (!background) setError(getErrorMessage(e));
    } finally {
      if (mounted.current && !background) setLoading(false);
    }
  }, []);

  useEffect(() => {
    mounted.current = true;
    if (!isAdmin) {
      setLoading(false);
      return () => {
        mounted.current = false;
      };
    }
    void load();
    // Everything here is time-relative — poll ages, and the warning that fires
    // when the pinned host goes quiet. Fetched once, the ages freeze at whatever
    // they were when the tab was opened and the warning can never appear for a
    // machine that dies afterwards, which is exactly when an operator is
    // looking at this page.
    const timer = setInterval(() => void load(true), REFRESH_MS);
    return () => {
      mounted.current = false;
      clearInterval(timer);
    };
  }, [load, isAdmin]);

  // Every action returns the whole pool, so they all end the same way.
  const run = useCallback(
    async (
      action: () => Promise<{ hosts: AgentRunnerHost[]; active_host: string | null }>,
      ok: string,
    ) => {
      setBusy(true);
      setNotice(null);
      try {
        const next = await action();
        if (!mounted.current) return;
        setHosts(next.hosts);
        setActiveHost(next.active_host);
        setNotice(ok);
      } catch (e) {
        if (mounted.current) setNotice(getErrorMessage(e));
      } finally {
        if (mounted.current) setBusy(false);
      }
    },
    [],
  );

  const handlePin = (host: string | null) =>
    run(
      () => setActiveAgentRunnerHost(host),
      host
        ? `New agent jobs now go to ${host}. Jobs already running elsewhere keep running there.`
        : 'Agent jobs unpinned — any runner may claim.',
    );

  const handleRemove = (host: string, slotsUsed: number) => {
    // The unforced call 409s on a busy host, and the operator would then have
    // to decide anyway — with the consequence stated, before the request.
    if (slotsUsed > 0) {
      const ok = window.confirm(
        `${host} is still running ${slotsUsed} attempt(s). Removing it does not stop them — ` +
          'nothing here can reach them. They are left to their leases, which expire and requeue ' +
          'the work exactly once. Remove anyway?',
      );
      if (!ok) return;
    }
    void run(() => removeAgentRunnerHost(host, slotsUsed > 0), `Removed ${host} from the pool.`);
  };

  const handleEnroll = async () => {
    const host = enrollHost.trim();
    if (!host) return;
    setBusy(true);
    setNotice(null);
    try {
      const issued = await issueHostEnrollToken(host, ENROLL_TTL_SECONDS);
      if (!mounted.current) return;
      setIssuedToken({ host: issued.host, token: issued.enroll_token });
      setEnrollHost('');
    } catch (e) {
      if (mounted.current) setNotice(getErrorMessage(e));
    } finally {
      if (mounted.current) setBusy(false);
    }
  };

  if (!isAdmin) {
    return (
      <div className="flex flex-1 flex-col items-center justify-center gap-3 p-8">
        <h1 className="text-xl font-semibold text-gray-900 dark:text-gray-50">
          Admin access required
        </h1>
        <p className="text-sm text-gray-500 dark:text-gray-400">
          The runner host pool is visible to admins only.
        </p>
      </div>
    );
  }

  // Only the pinned host's silence is an incident: nothing else can pick the
  // queue up, because pinning is what turned the others off.
  const staleActive = hosts.find((h) => h.active && h.seconds_since_seen > RECENT_POLL_SECONDS);

  return (
    <div className="flex-1 overflow-y-auto p-6">
      <div className="mx-auto max-w-5xl">
        <h1 className="text-[17px] font-semibold text-gray-900 dark:text-gray-50">Runner hosts</h1>
        <p className="mt-1 max-w-3xl text-[12px] text-gray-500 dark:text-gray-400">
          The machines that may run agent jobs. A host appears here once a runner on it has reported
          in, so this is the set that has polled — not a claim that any of them is reachable right
          now. Pinning is not a drain: the new host starts claiming immediately, while jobs already
          running on the old one keep running to the end.
        </p>

        {error && (
          <div className="mt-4 rounded-lg bg-red-50 dark:bg-red-500/10 px-3 py-2 text-[12px] text-red-600 dark:text-red-400">
            {error}
            <button
              type="button"
              onClick={() => void load()}
              className="ml-2 font-medium underline"
            >
              Retry
            </button>
          </div>
        )}

        {notice && (
          <div
            className="mt-4 rounded-lg bg-gray-50 dark:bg-gray-950 px-3 py-2 text-[12px] text-gray-700 dark:text-gray-300"
            role="status"
          >
            {notice}
          </div>
        )}

        {staleActive && (
          <div className="mt-4 rounded-lg bg-amber-50 dark:bg-amber-500/10 px-3 py-2 text-[12px] text-amber-800 dark:text-amber-300">
            {staleActive.host} last polled {formatAge(staleActive.seconds_since_seen)}. Jobs are
            pinned to it, so if its runner is down the queue is stopped — pin another host or choose
            Any host. (A machine that is simply busy also stops polling, so check before switching.)
          </div>
        )}

        <div className="mt-5 flex items-center gap-3">
          <button
            type="button"
            disabled={busy || activeHost === null}
            onClick={() => void handlePin(null)}
            className="rounded-md border border-gray-200 dark:border-white/10 px-2.5 py-1.5 text-[12px] font-medium text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-white/5 disabled:opacity-40"
          >
            Any host
          </button>
          <span className="text-[12px] text-gray-500 dark:text-gray-400">
            {activeHost ? `Pinned to ${activeHost}` : 'Unpinned — any runner may claim'}
          </span>
        </div>

        {loading ? (
          <p className="mt-6 text-[12px] text-gray-500 dark:text-gray-400">Loading…</p>
        ) : hosts.length === 0 ? (
          <p className="mt-6 text-[12px] text-gray-500 dark:text-gray-400">
            No runner has polled yet. Start one and it will appear here.
          </p>
        ) : (
          <div className="mt-4 overflow-x-auto">
            <table className="min-w-full text-left text-[12px]">
              <thead className="text-gray-500 dark:text-gray-400">
                <tr>
                  <th className="py-2 pr-4 font-medium">Host</th>
                  <th className="py-2 pr-4 font-medium">Status</th>
                  <th className="py-2 pr-4 font-medium">Slots</th>
                  <th className="py-2 pr-4 font-medium">Capabilities</th>
                  <th className="py-2 pr-4 font-medium">Runner</th>
                  <th className="py-2 pr-4 font-medium">Last polled</th>
                  <th className="py-2 pr-4 font-medium">Actions</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-100 dark:divide-white/5">
                {hosts.map((host) => (
                  <tr key={host.host} className="align-top">
                    <td className="py-2.5 pr-4">
                      <div className="flex items-center gap-2">
                        <span className="font-medium text-gray-900 dark:text-gray-50">
                          {host.host}
                        </span>
                        {host.active && (
                          <span className="rounded-full bg-crimson/[0.08] dark:bg-crimson/20 px-2 py-0.5 text-[11px] text-crimson dark:text-crimson-light">
                            pinned
                          </span>
                        )}
                      </div>
                      <div className="mt-0.5 text-[11px] text-gray-500 dark:text-gray-400">
                        {/* Which door the machine came through decides what its
                            name is worth: reported by the runner (a scheduling
                            label anything with the dispatcher credential can
                            claim) or bound to a credential by enrollment. */}
                        {host.enrolled ? 'enrolled' : 'self-reported name'}
                        {host.last_worker_id ? ` · ${host.last_worker_id}` : ''}
                      </div>
                    </td>
                    <td className="py-2.5 pr-4">
                      <StatusBadge status={host.status} />
                    </td>
                    <td className="py-2.5 pr-4 text-gray-700 dark:text-gray-300">
                      {formatSlots(host)}
                    </td>
                    <td className="py-2.5 pr-4 text-gray-700 dark:text-gray-300">
                      {host.capabilities.length > 0 ? host.capabilities.join(', ') : '—'}
                    </td>
                    <td className="py-2.5 pr-4 text-gray-700 dark:text-gray-300">
                      {host.runner_version ?? '—'}
                    </td>
                    <td className="py-2.5 pr-4 text-gray-700 dark:text-gray-300">
                      {formatAge(host.seconds_since_seen)}
                    </td>
                    <td className="py-2.5 pr-4">
                      <div className="flex flex-wrap gap-2">
                        {!host.active && (
                          <button
                            type="button"
                            disabled={busy}
                            onClick={() => void handlePin(host.host)}
                            className="rounded-md border border-gray-200 dark:border-white/10 px-2 py-1 font-medium text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-white/5 disabled:opacity-40"
                          >
                            Pin
                          </button>
                        )}
                        {host.status === 'draining' ? (
                          <button
                            type="button"
                            disabled={busy}
                            onClick={() =>
                              void run(
                                () => undrainAgentRunnerHost(host.host),
                                `${host.host} is taking work again.`,
                              )
                            }
                            className="rounded-md border border-gray-200 dark:border-white/10 px-2 py-1 font-medium text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-white/5 disabled:opacity-40"
                          >
                            Undrain
                          </button>
                        ) : (
                          <button
                            type="button"
                            disabled={busy}
                            onClick={() =>
                              void run(
                                () => drainAgentRunnerHost(host.host),
                                `${host.host} stops taking new work; running attempts finish.`,
                              )
                            }
                            className="rounded-md border border-gray-200 dark:border-white/10 px-2 py-1 font-medium text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-white/5 disabled:opacity-40"
                          >
                            Drain
                          </button>
                        )}
                        <button
                          type="button"
                          disabled={busy}
                          onClick={() => handleRemove(host.host, host.slots_used)}
                          className="rounded-md border border-gray-200 dark:border-white/10 px-2 py-1 font-medium text-red-600 dark:text-red-400 hover:bg-red-50 dark:hover:bg-red-500/10 disabled:opacity-40"
                        >
                          Remove
                        </button>
                        {!host.active && (
                          <button
                            type="button"
                            disabled={busy}
                            onClick={() =>
                              void run(
                                () => forgetAgentRunnerHost(host.host),
                                `Dropped ${host.host} from the list. A runner still polling there re-adds itself.`,
                              )
                            }
                            className="rounded-md px-2 py-1 font-medium text-gray-500 dark:text-gray-400 hover:bg-gray-50 dark:hover:bg-white/5 disabled:opacity-40"
                          >
                            Forget
                          </button>
                        )}
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        <div className="mt-8 rounded-xl border border-gray-200 dark:border-white/10 bg-white dark:bg-gray-900 p-5">
          <h2 className="text-[14px] font-semibold text-gray-900 dark:text-gray-50">
            Enroll a host
          </h2>
          <p className="mt-1 max-w-3xl text-[12px] text-gray-500 dark:text-gray-400">
            Mints a one-time token for the machine named here. The name is chosen on this side
            because registration upserts: a name already in use has that host&apos;s credential
            revoked and its row reset, so a typo can evict a working machine.
          </p>
          <div className="mt-3 flex flex-wrap items-center gap-2">
            <label htmlFor="enroll-host" className="sr-only">
              Host name
            </label>
            <input
              id="enroll-host"
              value={enrollHost}
              onChange={(e) => setEnrollHost(e.target.value)}
              placeholder="runner-b"
              className="rounded-md border border-gray-200 dark:border-white/10 px-2.5 py-1.5 text-[12px] text-gray-900 dark:text-gray-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-crimson/25"
            />
            <button
              type="button"
              disabled={busy || enrollHost.trim() === ''}
              onClick={() => void handleEnroll()}
              className="rounded-md bg-gray-900 dark:bg-gray-100 px-2.5 py-1.5 text-[12px] font-medium text-white dark:text-gray-900 hover:bg-gray-800 dark:hover:bg-white disabled:opacity-40"
            >
              Issue token
            </button>
          </div>

          {issuedToken && (
            <div className="mt-3 rounded-lg bg-amber-50 dark:bg-amber-500/10 px-3 py-2 text-[12px] text-amber-900 dark:text-amber-300">
              <p className="font-medium">
                Token for {issuedToken.host} — copy it now, it is not shown again.
              </p>
              {/* Only its hash is stored, so there is no call that can recover
                  it. An operator who loses it issues another. */}
              <code className="mt-1 block break-all font-mono text-[11px]">
                {issuedToken.token}
              </code>
              <button
                type="button"
                onClick={() => setIssuedToken(null)}
                className="mt-2 font-medium underline"
              >
                Dismiss
              </button>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
