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
import type { AgentRunnerHost, AgentRunnerHostPool } from '@/lib/api/admin';
import {
  drainAgentRunnerHost,
  forgetAgentRunnerHost,
  getAgentRunnerHosts,
  issueHostEnrollToken,
  removeAgentRunnerHost,
  setActiveAgentRunnerHost,
  setAgentRuntimeVersion,
  undrainAgentRunnerHost,
} from '@/lib/api/admin';
import { getErrorMessage } from '@/lib/utils/errors';

import {
  describeMenu,
  formatAge,
  formatImage,
  formatSlots,
  formatVersions,
  runtimeMenu,
} from './format';

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
  const [runtimeVersions, setRuntimeVersions] = useState<Record<string, string>>({});
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
      setRuntimeVersions(next.runtime_versions ?? {});
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
  const run = useCallback(async (action: () => Promise<AgentRunnerHostPool>, ok: string) => {
    setBusy(true);
    setNotice(null);
    try {
      const next = await action();
      if (!mounted.current) return;
      setHosts(next.hosts);
      setActiveHost(next.active_host);
      setRuntimeVersions(next.runtime_versions ?? {});
      setNotice(ok);
    } catch (e) {
      if (mounted.current) setNotice(getErrorMessage(e));
    } finally {
      if (mounted.current) setBusy(false);
    }
  }, []);

  const handlePin = (host: string | null) =>
    run(
      () => setActiveAgentRunnerHost(host),
      host
        ? `New agent jobs now go to ${host}. Jobs already running elsewhere keep running there.`
        : 'Agent jobs unpinned — any runner may claim.',
    );

  // Unioned across the pool, because the selection is deployment-wide while
  // the menus are per-image. Derived on render rather than held in state: it is
  // a view of `hosts`, and a second copy could disagree with the table beside
  // it after a poll.
  // Choosable first: the point of the section is the runtime an operator can
  // actually move, and alphabetical order buried claude-code under codex.
  const menu = runtimeMenu(hosts).sort(
    ([, a], [, b]) => Number(b.length > 1) - Number(a.length > 1),
  );

  const handleVersion = (runtime: string, version: string) =>
    run(
      () => setAgentRuntimeVersion(runtime, version || null),
      version
        ? `New ${runtime} threads will run ${version}. Threads already running keep theirs.`
        : `${runtime} is back on each host's image default.`,
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
    <div className="flex-1 overflow-y-auto">
      {/* The page shell the rest of the app uses (see IntegrationsView): one
          wide column, a real heading, and sections that are cards rather than
          controls floating between paragraphs. This page had grown its own
          denser dialect, which read as unfinished beside every other screen. */}
      <section className="mx-auto w-full max-w-5xl px-6 py-12 lg:px-12 lg:py-16">
        <h1 className="text-3xl font-semibold tracking-tight text-gray-900 dark:text-gray-50">
          Runner hosts
        </h1>
        <p className="mt-2 text-base text-gray-500 dark:text-gray-400">
          The machines that may run agent jobs, and which agent CLI they run it with.
        </p>

        {error && (
          <div className="mt-6 flex items-center justify-between gap-4 rounded-xl border border-red-200 dark:border-red-500/25 bg-red-50 dark:bg-red-500/10 px-4 py-3 text-sm text-red-700 dark:text-red-400">
            <span>{error}</span>
            <button
              type="button"
              onClick={() => void load()}
              className="shrink-0 font-medium hover:underline"
            >
              Retry
            </button>
          </div>
        )}

        {notice && (
          <div
            className="mt-6 rounded-xl border border-gray-200 dark:border-white/10 bg-gray-50 dark:bg-white/5 px-4 py-3 text-sm text-gray-700 dark:text-gray-300"
            role="status"
          >
            {notice}
          </div>
        )}

        {staleActive && (
          <div className="mt-6 rounded-xl border border-amber-200 dark:border-amber-500/25 bg-amber-50 dark:bg-amber-500/10 px-4 py-3 text-sm text-amber-800 dark:text-amber-300">
            {staleActive.host} last polled {formatAge(staleActive.seconds_since_seen)}. Jobs are
            pinned to it, so if its runner is down the queue is stopped — pin another host or choose
            Any host. (A machine that is simply busy also stops polling, so check before switching.)
          </div>
        )}

        {/* ── Where new jobs run ───────────────────────────────────────
            Its own section because it is a *policy*, not a property of any
            one row: inline beside the pool it read as a caption on the table.
            The pin itself still lives per-host, where the choice is made.

            Titled for the effect rather than the mechanism. "Claiming" is this
            codebase's word — a runner claims from the queue — and the first
            person to read it on the page asked what it meant. Not "assignment"
            either: nothing is assigned, because runners pull. */}
        <div className="mt-10">
          <h2 className="text-sm font-medium text-gray-600 dark:text-gray-400">
            Where new jobs run
          </h2>
          <div className="mt-4 rounded-2xl border border-gray-200 dark:border-white/10 bg-white dark:bg-gray-900 px-6 shadow-sm">
            <div className="flex min-h-20 flex-col justify-center gap-3 py-5 sm:flex-row sm:items-center">
              <div className="min-w-0 flex-1">
                <h3 className="font-medium text-gray-900 dark:text-gray-50">
                  {activeHost ? `Pinned to ${activeHost}` : 'Any host may claim'}
                </h3>
                <p className="mt-0.5 text-sm leading-5 text-gray-500 dark:text-gray-400">
                  {activeHost
                    ? 'New jobs go there from its next poll. Work already running elsewhere finishes where it started — pinning is not a drain.'
                    : 'Every host below that is online takes work. Pin one from the table to send new jobs to it alone.'}
                </p>
              </div>
              <div className="flex shrink-0 justify-end">
                <button
                  type="button"
                  disabled={busy || activeHost === null}
                  onClick={() => void handlePin(null)}
                  className="inline-flex items-center rounded-lg border border-gray-200 dark:border-white/10 bg-white dark:bg-gray-900 px-3 py-1.5 text-sm font-medium text-gray-800 dark:text-gray-200 shadow-sm hover:bg-gray-50 dark:hover:bg-white/5 disabled:opacity-40 disabled:hover:bg-white dark:disabled:hover:bg-gray-900"
                >
                  Any host
                </button>
              </div>
            </div>
          </div>
        </div>

        {/* ── Harness version ──────────────────────────────────────────
            One row per runtime, because the previous inline strip put five
            selects and five bare runtime names on a single line: five copies
            of "Image default" with nothing to say which was which, and no
            room to explain that the options come from the image.

            Two layers the wording has to keep apart: the *menu* is built into
            each sandbox image and changes only by rebuilding; this picks which
            item on it new work runs. A runtime with one option is an image
            holding one version — add more through that host's
            `*_EXTRA_VERSIONS`. */}
        {menu.length > 0 && (
          <div className="mt-10">
            <h2 className="text-sm font-medium text-gray-600 dark:text-gray-400">
              Harness version
            </h2>
            <p className="mt-1 text-sm text-gray-500 dark:text-gray-400">
              Applies to new threads. A conversation already running keeps the version it started
              on.
            </p>
            <div className="mt-4 rounded-2xl border border-gray-200 dark:border-white/10 bg-white dark:bg-gray-900 px-6 shadow-sm">
              {menu.map(([runtime, versions], index) => {
                // A runtime whose image holds one version has nothing to weigh
                // up, so it gets one line instead of three. Four such rows,
                // each explaining at paragraph length that there was no choice,
                // buried the one runtime that had one.
                const choosable = versions.length > 1;
                return (
                  <div
                    key={runtime}
                    className={`flex flex-col justify-center gap-3 sm:flex-row sm:items-center ${
                      choosable ? 'py-4' : 'py-2.5'
                    } ${index > 0 ? 'border-t border-gray-100 dark:border-white/5' : ''}`}
                  >
                    <div className="min-w-0 flex-1">
                      <label
                        htmlFor={`harness-version-${runtime}`}
                        className={
                          choosable
                            ? 'font-medium text-gray-900 dark:text-gray-50'
                            : 'text-sm text-gray-700 dark:text-gray-300'
                        }
                      >
                        {runtime}
                      </label>
                      {choosable ? (
                        <p className="mt-0.5 text-sm leading-5 text-gray-500 dark:text-gray-400">
                          {describeMenu(versions)}
                        </p>
                      ) : (
                        <span className="ml-2 text-sm text-gray-400 dark:text-gray-500">
                          {describeMenu(versions)}
                        </span>
                      )}
                    </div>
                    <div className="flex shrink-0 justify-end">
                      <select
                        id={`harness-version-${runtime}`}
                        disabled={busy}
                        value={runtimeVersions[runtime] ?? ''}
                        onChange={(event) => void handleVersion(runtime, event.target.value)}
                        className="rounded-lg border border-gray-200 dark:border-white/10 bg-white dark:bg-gray-900 px-3 py-1.5 text-sm font-medium text-gray-800 dark:text-gray-200 shadow-sm hover:bg-gray-50 dark:hover:bg-white/5 disabled:opacity-40"
                      >
                        <option value="">Image default</option>
                        {versions.map((version) => (
                          <option key={version} value={version}>
                            {version}
                          </option>
                        ))}
                      </select>
                    </div>
                  </div>
                );
              })}
            </div>
          </div>
        )}

        {/* ── The pool ─────────────────────────────────────────────────
            Six columns, not nine. `Capabilities` and `Runner` were empty on
            every real row and still took a column each; they now sit in the
            host's own meta line and appear only when there is something to
            say. What a machine will *run* — CLI versions and the image they
            came from — is one question, so it is one cell. */}
        <div className="mt-10">
          <h2 className="text-sm font-medium text-gray-600 dark:text-gray-400">Pool</h2>
          <p className="mt-1 text-sm text-gray-500 dark:text-gray-400">
            A host appears once its runner has polled — which is not a claim that it is reachable
            now.
          </p>

          {loading ? (
            <p className="mt-4 text-sm text-gray-500 dark:text-gray-400">Loading…</p>
          ) : hosts.length === 0 ? (
            <div className="mt-4 rounded-2xl border border-gray-200 dark:border-white/10 bg-white dark:bg-gray-900 px-6 py-8 text-center text-sm text-gray-500 dark:text-gray-400 shadow-sm">
              No runner has polled yet. Start one and it will appear here.
            </div>
          ) : (
            <div className="mt-4 overflow-hidden rounded-2xl border border-gray-200 dark:border-white/10 bg-white dark:bg-gray-900 shadow-sm">
              <div className="overflow-x-auto">
                <table className="min-w-full text-left text-sm">
                  <thead className="border-b border-gray-100 dark:border-white/5 text-xs text-gray-500 dark:text-gray-400">
                    <tr>
                      <th className="px-6 py-3 font-medium">Host</th>
                      <th className="px-3 py-3 font-medium">Status</th>
                      <th className="px-3 py-3 font-medium">Slots</th>
                      <th className="px-3 py-3 font-medium">Runs</th>
                      <th className="px-3 py-3 font-medium whitespace-nowrap">Last polled</th>
                      <th className="px-6 py-3 text-right font-medium">Actions</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-gray-100 dark:divide-white/5">
                    {hosts.map((host) => (
                      <tr key={host.host} className="align-top">
                        <td className="px-6 py-4">
                          <div className="flex items-center gap-2">
                            <span className="font-medium text-gray-900 dark:text-gray-50">
                              {host.host}
                            </span>
                            {host.active && (
                              <span className="rounded-full bg-crimson/[0.08] dark:bg-crimson/20 px-2 py-0.5 text-xs font-medium text-crimson dark:text-crimson-light">
                                pinned
                              </span>
                            )}
                          </div>
                          <div className="mt-1 text-xs leading-5 text-gray-500 dark:text-gray-400">
                            {/* Which door the machine came through decides what
                                its name is worth: reported by the runner (a
                                scheduling label anything with the dispatcher
                                credential can claim) or bound to a credential
                                by enrollment. */}
                            {host.enrolled ? 'enrolled' : 'self-reported name'}
                            {host.last_worker_id ? ` · ${host.last_worker_id}` : ''}
                            {host.runner_version ? ` · runner ${host.runner_version}` : ''}
                            {host.capabilities.length > 0
                              ? ` · ${host.capabilities.join(', ')}`
                              : ''}
                          </div>
                        </td>
                        <td className="px-3 py-4">
                          <StatusBadge status={host.status} />
                        </td>
                        <td className="px-3 py-4 whitespace-nowrap text-gray-700 dark:text-gray-300">
                          {formatSlots(host)}
                        </td>
                        <td className="px-3 py-4">
                          <div className="text-gray-700 dark:text-gray-300">
                            {formatVersions(host)}
                          </div>
                          {/* The digest, not the tag: two hosts on
                              `...-job:staging` can be running different builds,
                              and that is exactly the confusion worth resolving
                              on sight. */}
                          {host.sandbox_image_digest && (
                            <div className="mt-1 font-mono text-xs text-gray-500 dark:text-gray-400">
                              {formatImage(host)}
                            </div>
                          )}
                        </td>
                        <td className="px-3 py-4 whitespace-nowrap text-gray-700 dark:text-gray-300">
                          {formatAge(host.seconds_since_seen)}
                        </td>
                        <td className="px-6 py-4">
                          {/* One line, right-aligned, and the destructive pair
                              carries no border: four equally-weighted buttons
                              wrapping onto two ragged lines made Remove the
                              loudest thing in the row. */}
                          <div className="flex items-center justify-end gap-1">
                            {!host.active && (
                              <button
                                type="button"
                                disabled={busy}
                                onClick={() => void handlePin(host.host)}
                                className="rounded-lg border border-gray-200 dark:border-white/10 bg-white dark:bg-gray-900 px-2.5 py-1 text-xs font-medium text-gray-800 dark:text-gray-200 shadow-sm hover:bg-gray-50 dark:hover:bg-white/5 disabled:opacity-40"
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
                                className="rounded-lg border border-gray-200 dark:border-white/10 bg-white dark:bg-gray-900 px-2.5 py-1 text-xs font-medium text-gray-800 dark:text-gray-200 shadow-sm hover:bg-gray-50 dark:hover:bg-white/5 disabled:opacity-40"
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
                                className="rounded-lg border border-gray-200 dark:border-white/10 bg-white dark:bg-gray-900 px-2.5 py-1 text-xs font-medium text-gray-800 dark:text-gray-200 shadow-sm hover:bg-gray-50 dark:hover:bg-white/5 disabled:opacity-40"
                              >
                                Drain
                              </button>
                            )}
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
                                className="rounded-lg px-2.5 py-1 text-xs font-medium text-gray-500 dark:text-gray-400 hover:bg-gray-50 dark:hover:bg-white/5 disabled:opacity-40"
                              >
                                Forget
                              </button>
                            )}
                            <button
                              type="button"
                              disabled={busy}
                              onClick={() => handleRemove(host.host, host.slots_used)}
                              className="rounded-lg px-2.5 py-1 text-xs font-medium text-red-600 dark:text-red-400 hover:bg-red-50 dark:hover:bg-red-500/10 disabled:opacity-40"
                            >
                              Remove
                            </button>
                          </div>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}
        </div>

        {/* ── Enroll ───────────────────────────────────────────────── */}
        <div className="mt-10">
          <h2 className="text-sm font-medium text-gray-600 dark:text-gray-400">Enroll a host</h2>
          <div className="mt-4 rounded-2xl border border-gray-200 dark:border-white/10 bg-white dark:bg-gray-900 px-6 py-5 shadow-sm">
            <p className="max-w-3xl text-sm leading-5 text-gray-500 dark:text-gray-400">
              Mints a one-time token for the machine named here. The name is chosen on this side
              because registration upserts: a name already in use has that host&apos;s credential
              revoked and its row reset, so a typo can evict a working machine.
            </p>
            <div className="mt-4 flex flex-wrap items-center gap-2">
              <label htmlFor="enroll-host" className="sr-only">
                Host name
              </label>
              <input
                id="enroll-host"
                value={enrollHost}
                onChange={(e) => setEnrollHost(e.target.value)}
                placeholder="runner-b"
                className="rounded-lg border border-gray-200 dark:border-white/10 bg-white dark:bg-gray-900 px-3 py-1.5 text-sm text-gray-900 dark:text-gray-50 shadow-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-crimson/25"
              />
              <button
                type="button"
                disabled={busy || enrollHost.trim() === ''}
                onClick={() => void handleEnroll()}
                className="rounded-lg bg-gray-900 dark:bg-gray-100 px-3 py-1.5 text-sm font-medium text-white dark:text-gray-900 shadow-sm hover:bg-gray-800 dark:hover:bg-white disabled:opacity-40"
              >
                Issue token
              </button>
            </div>

            {issuedToken && (
              <div className="mt-4 rounded-xl border border-amber-200 dark:border-amber-500/25 bg-amber-50 dark:bg-amber-500/10 px-4 py-3 text-sm text-amber-900 dark:text-amber-300">
                <p className="font-medium">
                  Token for {issuedToken.host} — copy it now, it is not shown again.
                </p>
                {/* Only its hash is stored, so there is no call that can recover
                    it. An operator who loses it issues another. */}
                <code className="mt-1 block break-all font-mono text-xs">{issuedToken.token}</code>
                <button
                  type="button"
                  onClick={() => setIssuedToken(null)}
                  className="mt-2 font-medium hover:underline"
                >
                  Dismiss
                </button>
              </div>
            )}
          </div>
        </div>
      </section>
    </div>
  );
}
