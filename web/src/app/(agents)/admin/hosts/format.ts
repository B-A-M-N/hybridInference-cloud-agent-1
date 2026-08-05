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
