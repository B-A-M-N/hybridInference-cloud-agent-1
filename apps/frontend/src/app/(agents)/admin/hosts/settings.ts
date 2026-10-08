// Runtime settings: which value a server should use, and whether it does.
//
// Precedence is the control plane's (`cloud_agent_shared/host_settings.py`): a
// value set for one server, then the value set for all servers, then the
// server's own `.env` or built-in default. The page only reads what the server
// reports applying and compares — it never assumes a saved value is in force,
// for the same reason the execution-slots cell says "applying" until the host
// confirms.

import type { AgentRunnerHost, HostSettingDefinition, HostSettingValues } from '@/lib/api/admin';
import { HOST_SETTINGS_CAPABILITY } from '@/lib/api/admin';

/** Where the value a server should use comes from. */
export type SettingSource = 'server' | 'global' | 'own';

/** Whether the server has taken that value up. */
export type SettingState = 'applied' | 'applying' | 'unreported' | 'unsupported';

export interface SettingFact {
  definition: HostSettingDefinition;
  /** The admin value that applies to this server, or null when none does. */
  admin: number | null;
  source: SettingSource;
  /** What the server reports using now, or null when it has not said. */
  inUse: number | null;
  /** The server's own value — its `.env` or the built-in default — or null when unreported. */
  own: number | null;
  state: SettingState;
}

/** Whether this server's runner applies settings from this page at all. */
export function supportsSettings(host: AgentRunnerHost): boolean {
  return (host.capabilities ?? []).includes(HOST_SETTINGS_CAPABILITY);
}

function valueOf(values: HostSettingValues | null | undefined, key: string): number | null {
  const value = values?.[key];
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

/** One setting on one server: the value it should use, where from, and whether it does. */
export function settingFact(
  definition: HostSettingDefinition,
  globalValues: HostSettingValues,
  host: AgentRunnerHost,
): SettingFact {
  const server = valueOf(host.settings_desired, definition.key);
  const global = valueOf(globalValues, definition.key);
  const admin = server ?? global;
  const source: SettingSource = server !== null ? 'server' : global !== null ? 'global' : 'own';
  const inUse = valueOf(host.settings_applied, definition.key);
  const own = valueOf(host.settings_local, definition.key);
  let state: SettingState;
  if (!supportsSettings(host)) {
    state = 'unsupported';
  } else if (inUse === null) {
    state = 'unreported';
  } else {
    // With no admin value the server should be back on its own; until it says
    // so, a cleared value is still being undone.
    const target = admin ?? own;
    state = target === null || inUse === target ? 'applied' : 'applying';
  }
  return { definition, admin, source, inUse, own, state };
}

export interface GlobalRollout {
  /** Servers taking this setting from the global value, and applying it. */
  applied: number;
  /** Servers taking it from the global value that have not applied it yet. */
  waiting: number;
  /** Servers with a value of their own, which wins over the global one. */
  overridden: number;
  /** Servers whose runner predates settings and keeps its `.env`. */
  unsupported: number;
}

/** How far the global value of one setting has reached across the servers. */
export function globalRollout(
  definition: HostSettingDefinition,
  globalValues: HostSettingValues,
  hosts: AgentRunnerHost[],
): GlobalRollout {
  const rollout: GlobalRollout = { applied: 0, waiting: 0, overridden: 0, unsupported: 0 };
  for (const host of hosts) {
    const fact = settingFact(definition, globalValues, host);
    if (fact.state === 'unsupported') rollout.unsupported += 1;
    else if (fact.source === 'server') rollout.overridden += 1;
    else if (fact.state === 'applied') rollout.applied += 1;
    else rollout.waiting += 1;
  }
  return rollout;
}

function plural(count: number, one: string, many: string): string {
  return `${count} ${count === 1 ? one : many}`;
}

/** A value in its setting's unit, in the largest whole unit that fits. */
export function formatSettingValue(definition: HostSettingDefinition, value: number): string {
  if (definition.unit === 'seconds') {
    if (Number.isInteger(value) && value > 0) {
      if (value % 86_400 === 0) return `${value / 86_400} d`;
      if (value % 3_600 === 0) return `${value / 3_600} h`;
      if (value % 60 === 0) return `${value / 60} min`;
    }
    return `${value} s`;
  }
  if (definition.unit === 'MiB') {
    return Number.isInteger(value) && value > 0 && value % 1024 === 0
      ? `${value / 1024} GiB`
      : `${value} MiB`;
  }
  return String(value);
}

/** The unit a typed number is in, as the input's suffix. */
export function unitLabel(definition: HostSettingDefinition): string {
  return definition.unit === 'count' ? '' : definition.unit === 'seconds' ? 's' : 'MiB';
}

/** Read a typed value: a whole number inside the setting's bounds, or why not. */
export function parseSettingInput(
  definition: HostSettingDefinition,
  raw: string,
): { value: number } | { error: string } {
  const text = raw.trim();
  const value = Number(text);
  if (
    text === '' ||
    !Number.isInteger(value) ||
    value < definition.minimum ||
    value > definition.maximum
  ) {
    const unit = unitLabel(definition);
    return {
      error: `Enter a whole number from ${definition.minimum} to ${definition.maximum}${unit ? ` ${unit}` : ''}.`,
    };
  }
  return { value };
}

/** A one-line account of a global value's reach, for under its control. */
export function rolloutText(
  definition: HostSettingDefinition,
  globalValues: HostSettingValues,
  hosts: AgentRunnerHost[],
): string {
  const rollout = globalRollout(definition, globalValues, hosts);
  const parts: string[] = [];
  if (valueOf(globalValues, definition.key) === null) {
    parts.push(
      `Each server uses its own .env value, or ${formatSettingValue(definition, definition.default)}`,
    );
  } else {
    const following = rollout.applied + rollout.waiting;
    parts.push(
      rollout.waiting === 0
        ? `In use on ${plural(following, 'server', 'servers')}`
        : `Applied on ${rollout.applied} of ${plural(following, 'server', 'servers')}`,
    );
  }
  if (rollout.overridden > 0) {
    parts.push(plural(rollout.overridden, 'server sets its own', 'servers set their own'));
  }
  if (rollout.unsupported > 0) {
    parts.push(`${plural(rollout.unsupported, 'server needs', 'servers need')} a redeploy`);
  }
  return parts.join(' · ');
}

/** What a server-level row says about where its value comes from and whether it is in force. */
export function factNote(fact: SettingFact): string {
  if (fact.state === 'unsupported') return 'Redeploy this server to manage it here';
  const from =
    fact.source === 'server'
      ? 'set for this server'
      : fact.source === 'global'
        ? 'set for all servers'
        : 'server default';
  if (fact.state === 'unreported') return `${from} · not reported yet`;
  if (fact.state === 'applying') {
    const target = fact.admin ?? fact.own;
    return target === null
      ? `${from} · applying`
      : `${from} · applying ${formatSettingValue(fact.definition, target)}`;
  }
  return from;
}
