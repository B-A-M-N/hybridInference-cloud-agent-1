import { describe, expect, it } from 'vitest';

import type { AgentRunnerHost, HostSettingDefinition } from '@/lib/api/admin';

import {
  factNote,
  formatSettingValue,
  globalRollout,
  parseSettingInput,
  rolloutText,
  settingFact,
} from './settings';

const TIMEOUT: HostSettingDefinition = {
  key: 'agent_timeout_s',
  env: 'AGENT_TIMEOUT_S',
  label: 'Turn time limit',
  description: 'How long one agent turn may run.',
  unit: 'seconds',
  default: 3600,
  minimum: 60,
  maximum: 86400,
  scope: 'runner',
};

const IMPORT: HostSettingDefinition = {
  ...TIMEOUT,
  key: 'source_import_max_mib',
  env: 'AGENT_SOURCE_IMPORT_MAX_MIB',
  label: 'Repository import limit',
  unit: 'MiB',
  default: 1024,
  minimum: 1,
  maximum: 65536,
};

const SESSIONS: HostSettingDefinition = {
  ...TIMEOUT,
  key: 'terminal_max_sessions',
  unit: 'count',
  default: 64,
  minimum: 1,
  maximum: 4096,
  scope: 'broker',
};

function host(over: Partial<AgentRunnerHost> = {}): AgentRunnerHost {
  return {
    host: 'runner-a',
    active: false,
    last_worker_id: null,
    first_seen_at: '2026-09-11T12:00:00Z',
    last_seen_at: '2026-09-11T12:00:00Z',
    seconds_since_seen: 1,
    status: 'online',
    enrolled: true,
    labels: {},
    capabilities: ['host-settings-v1'],
    slots_total: 3,
    slots_used: 0,
    runner_version: null,
    broker_url: null,
    last_heartbeat_at: null,
    sandbox_runtime: null,
    sandbox_image_digest: null,
    runtime_versions: null,
    runtime_versions_available: null,
    capacity_epoch: null,
    capacity: null,
    settings_desired: {},
    settings_applied: { agent_timeout_s: 3600 },
    settings_local: { agent_timeout_s: 3600 },
    ...over,
  };
}

describe('settingFact', () => {
  it('uses the server value over the global one, and the host own with neither', () => {
    expect(
      settingFact(
        TIMEOUT,
        { agent_timeout_s: 7200 },
        host({ settings_desired: { agent_timeout_s: 900 } }),
      ),
    ).toMatchObject({ admin: 900, source: 'server' });
    expect(settingFact(TIMEOUT, { agent_timeout_s: 7200 }, host())).toMatchObject({
      admin: 7200,
      source: 'global',
    });
    expect(settingFact(TIMEOUT, {}, host())).toMatchObject({
      admin: null,
      source: 'own',
      own: 3600,
      state: 'applied',
    });
  });

  it('says applying until the server reports the value it should use', () => {
    const pending = settingFact(TIMEOUT, { agent_timeout_s: 7200 }, host());
    expect(pending).toMatchObject({ inUse: 3600, state: 'applying' });
    expect(factNote(pending)).toBe('set for all servers · applying 2 h');

    const done = settingFact(
      TIMEOUT,
      { agent_timeout_s: 7200 },
      host({ settings_applied: { agent_timeout_s: 7200 } }),
    );
    expect(done.state).toBe('applied');
    expect(factNote(done)).toBe('set for all servers');
  });

  it('treats a cleared value as applying until the server is back on its own', () => {
    const fact = settingFact(TIMEOUT, {}, host({ settings_applied: { agent_timeout_s: 7200 } }));
    expect(fact.state).toBe('applying');
    expect(factNote(fact)).toBe('server default · applying 1 h');
  });

  it('never claims a value for a server that has not reported, or cannot apply it', () => {
    expect(
      settingFact(TIMEOUT, { agent_timeout_s: 7200 }, host({ settings_applied: null })).state,
    ).toBe('unreported');
    const old = settingFact(TIMEOUT, { agent_timeout_s: 7200 }, host({ capabilities: [] }));
    expect(old.state).toBe('unsupported');
    expect(factNote(old)).toBe('Redeploy this server to manage it here');
  });
});

describe('globalRollout', () => {
  it('counts servers applying the global value apart from those overriding it', () => {
    const hosts = [
      host({ host: 'a', settings_applied: { agent_timeout_s: 7200 } }),
      host({ host: 'b' }),
      host({ host: 'c', settings_desired: { agent_timeout_s: 900 } }),
      host({ host: 'd', capabilities: [] }),
    ];
    expect(globalRollout(TIMEOUT, { agent_timeout_s: 7200 }, hosts)).toEqual({
      applied: 1,
      waiting: 1,
      overridden: 1,
      unsupported: 1,
    });
    expect(rolloutText(TIMEOUT, { agent_timeout_s: 7200 }, hosts)).toBe(
      'Applied on 1 of 2 servers · 1 server sets its own · 1 server needs a redeploy',
    );
  });

  it('says each server uses its own when nothing is set globally', () => {
    expect(rolloutText(TIMEOUT, {}, [host()])).toBe('Each server uses its own .env value, or 1 h');
    expect(rolloutText(TIMEOUT, { agent_timeout_s: 3600 }, [host(), host({ host: 'b' })])).toBe(
      'In use on 2 servers',
    );
  });
});

describe('formatSettingValue', () => {
  it('uses the largest whole unit', () => {
    expect(formatSettingValue(TIMEOUT, 86400)).toBe('1 d');
    expect(formatSettingValue(TIMEOUT, 28800)).toBe('8 h');
    expect(formatSettingValue(TIMEOUT, 1800)).toBe('30 min');
    expect(formatSettingValue(TIMEOUT, 90)).toBe('90 s');
    expect(formatSettingValue(TIMEOUT, 15.5)).toBe('15.5 s');
    expect(formatSettingValue(IMPORT, 4096)).toBe('4 GiB');
    expect(formatSettingValue(IMPORT, 1500)).toBe('1500 MiB');
    expect(formatSettingValue(SESSIONS, 64)).toBe('64');
  });
});

describe('parseSettingInput', () => {
  it('accepts a whole number in bounds and explains anything else', () => {
    expect(parseSettingInput(TIMEOUT, ' 7200 ')).toEqual({ value: 7200 });
    for (const raw of ['', '59', '86401', '90.5', 'abc']) {
      expect(parseSettingInput(TIMEOUT, raw)).toEqual({
        error: 'Enter a whole number from 60 to 86400 s.',
      });
    }
    expect(parseSettingInput(SESSIONS, '0')).toEqual({
      error: 'Enter a whole number from 1 to 4096.',
    });
  });
});
