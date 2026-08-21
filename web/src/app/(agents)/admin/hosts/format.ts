// Cell formatting for the host table.
//
// **A separate module because `page.tsx` may not export anything else.** Next
// type-checks a page's exports against a fixed shape, so a named helper
// exported beside the default component fails the build — and it fails at
// `next build`, not in lint, tests or `tsc --noEmit`. Tested directly here
// rather than through the rendered table, which is what the extra exports were
// for.

import type { AgentRunnerHost } from '@/lib/api/admin';

export function formatAge(seconds: number): string {
  if (seconds < 60) return `${Math.max(0, Math.round(seconds))}s ago`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  return `${Math.floor(hours / 24)}d ago`;
}

/** Slots as "2 / 4", or "2" where the host declares no ceiling. */
export function formatSlots(host: AgentRunnerHost): string {
  return host.slots_total === null
    ? `${host.slots_used}`
    : `${host.slots_used} / ${host.slots_total}`;
}

/**
 * The CLI versions a host reports, as "claude-code 2.1.220, codex 0.145.0".
 *
 * The *default* per runtime, which is what a job runs unselected — the menu it
 * could also run is a pool-wide question and lives in the selector above the
 * table, not in a cell repeated per row.
 */
export function formatVersions(host: AgentRunnerHost): string {
  const versions = host.runtime_versions;
  if (!versions || Object.keys(versions).length === 0) return '—';
  return Object.entries(versions)
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([runtime, version]) => `${runtime} ${version}`)
    .join(', ');
}

/**
 * The sandbox image, short enough to read.
 *
 * A digest is `name@sha256:<64 hex>` or a bare local id; either way the first
 * few characters identify it among the two or three an operator is comparing,
 * and the whole string would push every other column off the page.
 */
export function formatImage(host: AgentRunnerHost): string {
  const digest = host.sandbox_image_digest;
  if (!digest) return '—';
  const at = digest.lastIndexOf('@');
  const hash = at === -1 ? digest : digest.slice(at + 1);
  const short = hash.replace(/^sha256:/, '').slice(0, 12);
  return short || digest.slice(0, 12);
}

/**
 * Every (runtime, versions) pair the pool as a whole offers.
 *
 * Unioned across hosts because the selection is deployment-wide while the menus
 * are per-image: with two hosts on different images, a version only one of them
 * holds is still selectable — the claim falls back on the host that lacks it
 * rather than refusing, which is the documented behaviour and better than
 * hiding the option. Sorted so the list does not reshuffle between polls.
 */
export function runtimeMenu(hosts: AgentRunnerHost[]): [string, string[]][] {
  const menu = new Map<string, string[]>();
  for (const host of hosts) {
    for (const [runtime, versions] of Object.entries(host.runtime_versions_available ?? {})) {
      const seen = menu.get(runtime) ?? [];
      // Order is the image's — default first — and preserved for the host that
      // introduced each version, so the first option is a real default.
      menu.set(runtime, [...seen, ...versions.filter((v) => !seen.includes(v))]);
    }
  }
  return [...menu.entries()].sort(([a], [b]) => a.localeCompare(b));
}

/**
 * What a runtime's row says under its name.
 *
 * The menu is a property of the *image*, not of this page, and the selector
 * above it is easy to read as "pick any version you like". Naming the default
 * and what else is in there makes the ceiling visible without a paragraph
 * about `*_EXTRA_VERSIONS` next to every row.
 */
export function describeMenu(versions: string[]): string {
  if (versions.length === 0) return 'No version reported.';
  const [fallback, ...rest] = versions;
  if (rest.length === 0) return `The image holds only ${fallback}.`;
  return `Image default ${fallback} · also built in: ${rest.join(', ')}`;
}
