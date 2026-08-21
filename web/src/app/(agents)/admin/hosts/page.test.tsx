// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const auth = vi.hoisted(() => ({ role: 'admin' }));

vi.mock('@/components/providers', () => ({
  useAuth: () => ({ state: { user: { user_name: 'Ada', role: auth.role } } }),
}));

// The real ranking, not a stub that always says yes: the point of two of these
// cases is that a non-admin is refused, and a `hasRole` mocked to true would
// pass them while the page was wide open.
vi.mock('@/components/providers/AuthProvider', async () => {
  const actual = await vi.importActual<typeof import('@/components/providers/AuthProvider')>(
    '@/components/providers/AuthProvider',
  );
  return { hasRole: actual.hasRole };
});

vi.mock('@/lib/api/admin', () => ({
  getAgentRunnerHosts: vi.fn(),
  setActiveAgentRunnerHost: vi.fn(),
  forgetAgentRunnerHost: vi.fn(),
  drainAgentRunnerHost: vi.fn(),
  undrainAgentRunnerHost: vi.fn(),
  removeAgentRunnerHost: vi.fn(),
  issueHostEnrollToken: vi.fn(),
  setAgentRuntimeVersion: vi.fn(),
}));

import {
  drainAgentRunnerHost,
  getAgentRunnerHosts,
  issueHostEnrollToken,
  removeAgentRunnerHost,
  setActiveAgentRunnerHost,
  setAgentRuntimeVersion,
} from '@/lib/api/admin';

import { formatAge, formatImage, formatSlots, formatVersions, runtimeMenu } from './format';
import AdminHostsPage from './page';

const mocked = {
  list: getAgentRunnerHosts as unknown as ReturnType<typeof vi.fn>,
  pin: setActiveAgentRunnerHost as unknown as ReturnType<typeof vi.fn>,
  drain: drainAgentRunnerHost as unknown as ReturnType<typeof vi.fn>,
  remove: removeAgentRunnerHost as unknown as ReturnType<typeof vi.fn>,
  enroll: issueHostEnrollToken as unknown as ReturnType<typeof vi.fn>,
  setVersion: setAgentRuntimeVersion as unknown as ReturnType<typeof vi.fn>,
};

function host(over: Partial<Record<string, unknown>> = {}) {
  return {
    host: 'runner-a',
    active: false,
    last_worker_id: 'worker-1',
    first_seen_at: '2026-08-01T00:00:00Z',
    last_seen_at: '2026-08-05T00:00:00Z',
    seconds_since_seen: 12,
    status: 'online',
    enrolled: true,
    labels: {},
    capabilities: ['kata'],
    slots_total: 4,
    slots_used: 0,
    runner_version: '1.2.3',
    broker_url: 'http://runner-a:8092',
    last_heartbeat_at: '2026-08-05T00:00:00Z',
    sandbox_runtime: '',
    sandbox_image_digest: 'freeinference-cloud-agent-job@sha256:abcdef0123456789',
    runtime_versions: { 'claude-code': '2.1.220' },
    runtime_versions_available: { 'claude-code': ['2.1.220', '2.1.235'] },
    ...over,
  };
}

function pool(
  hosts: unknown[],
  active: string | null = null,
  runtimeVersions: Record<string, string> = {},
) {
  return { hosts, active_host: active, runtime_versions: runtimeVersions };
}

describe('AdminHostsPage', () => {
  beforeEach(() => {
    auth.role = 'admin';
    Object.values(mocked).forEach((m) => m.mockReset());
    mocked.list.mockResolvedValue(pool([host()]));
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  it('refuses a non-admin without asking the control plane', async () => {
    auth.role = 'internal';
    render(<AdminHostsPage />);

    expect(await screen.findByText('Admin access required')).toBeInTheDocument();
    // Not merely hidden: a page that renders the refusal *and* fetches has told
    // the operator no while doing the thing anyway.
    expect(mocked.list).not.toHaveBeenCalled();
  });

  it('renders the pool a host that joined by polling would produce', async () => {
    // Every dynamic-pool field null/empty, which is exactly what a pre-Phase-G
    // row looks like. This must render rather than 500 the page an operator
    // opens to fix that host.
    mocked.list.mockResolvedValue(
      pool([
        host({
          host: 'legacy-box',
          enrolled: false,
          capabilities: [],
          slots_total: null,
          runner_version: null,
        }),
      ]),
    );
    render(<AdminHostsPage />);

    expect(await screen.findByText('legacy-box')).toBeInTheDocument();
    // Rendered beside the worker id in one line, hence the substring match.
    expect(screen.getByText(/self-reported name/)).toBeInTheDocument();
    // No ceiling declared: the slot cell is the count alone, not "0 / null".
    expect(screen.getByText('0')).toBeInTheDocument();
  });

  it('pins a host and reports that running jobs stay where they are', async () => {
    mocked.pin.mockResolvedValue(pool([host({ active: true })], 'runner-a'));
    render(<AdminHostsPage />);

    fireEvent.click(await screen.findByRole('button', { name: 'Pin' }));

    await waitFor(() => expect(mocked.pin).toHaveBeenCalledWith('runner-a'));
    expect(await screen.findByRole('status')).toHaveTextContent(
      /Jobs already running elsewhere keep running there/,
    );
  });

  it('unpins through the null the API expects, not a sentinel string', async () => {
    mocked.list.mockResolvedValue(pool([host({ active: true })], 'runner-a'));
    mocked.pin.mockResolvedValue(pool([host()], null));
    render(<AdminHostsPage />);

    fireEvent.click(await screen.findByRole('button', { name: 'Any host' }));

    await waitFor(() => expect(mocked.pin).toHaveBeenCalledWith(null));
  });

  it('drains without touching the pin', async () => {
    mocked.drain.mockResolvedValue(pool([host({ status: 'draining' })]));
    render(<AdminHostsPage />);

    fireEvent.click(await screen.findByRole('button', { name: 'Drain' }));

    await waitFor(() => expect(mocked.drain).toHaveBeenCalledWith('runner-a'));
    expect(mocked.pin).not.toHaveBeenCalled();
    expect(await screen.findByRole('button', { name: 'Undrain' })).toBeInTheDocument();
  });

  it('removes a quiet host unforced', async () => {
    mocked.remove.mockResolvedValue(pool([]));
    render(<AdminHostsPage />);

    fireEvent.click(await screen.findByRole('button', { name: 'Remove' }));

    await waitFor(() => expect(mocked.remove).toHaveBeenCalledWith('runner-a', false));
  });

  it('makes the operator confirm before forcing a busy host out, and says why', async () => {
    mocked.list.mockResolvedValue(pool([host({ slots_used: 2 })]));
    const confirm = vi.fn().mockReturnValue(false);
    vi.stubGlobal('confirm', confirm);
    render(<AdminHostsPage />);

    fireEvent.click(await screen.findByRole('button', { name: 'Remove' }));

    expect(confirm).toHaveBeenCalledTimes(1);
    // The consequence has to be in the prompt: force does not stop the
    // attempts, it leaves them to their leases.
    expect(confirm.mock.calls[0][0]).toMatch(/does not stop them/);
    expect(mocked.remove).not.toHaveBeenCalled();

    confirm.mockReturnValue(true);
    mocked.remove.mockResolvedValue(pool([]));
    fireEvent.click(screen.getByRole('button', { name: 'Remove' }));
    await waitFor(() => expect(mocked.remove).toHaveBeenCalledWith('runner-a', true));
  });

  it('warns when the pinned host has gone quiet', async () => {
    mocked.list.mockResolvedValue(
      pool([host({ active: true, seconds_since_seen: 3600 })], 'runner-a'),
    );
    render(<AdminHostsPage />);

    expect(await screen.findByText(/the queue is stopped/)).toBeInTheDocument();
  });

  it('does not warn when an unpinned host is quiet — nothing depends on it', async () => {
    mocked.list.mockResolvedValue(pool([host({ seconds_since_seen: 3600 })], null));
    render(<AdminHostsPage />);

    await screen.findByText('runner-a');
    expect(screen.queryByText(/the queue is stopped/)).not.toBeInTheDocument();
  });

  it('shows an enrollment token once, with the warning that it is not recoverable', async () => {
    mocked.enroll.mockResolvedValue({
      enroll_token: 'tok-abc',
      host: 'runner-b',
      expires_at: '2026-08-05T01:00:00Z',
    });
    render(<AdminHostsPage />);

    fireEvent.change(await screen.findByLabelText('Host name'), {
      target: { value: 'runner-b' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Issue token' }));

    expect(await screen.findByText('tok-abc')).toBeInTheDocument();
    expect(screen.getByText(/not shown again/)).toBeInTheDocument();
    expect(mocked.enroll).toHaveBeenCalledWith('runner-b', 900);
  });

  it('keeps the last good pool when a background refresh fails', async () => {
    vi.useFakeTimers();
    try {
      mocked.list.mockResolvedValueOnce(pool([host()]));
      render(<AdminHostsPage />);
      await vi.waitFor(() => expect(screen.getByText('runner-a')).toBeInTheDocument());

      mocked.list.mockRejectedValue(new Error('gateway down'));
      await vi.advanceTimersByTimeAsync(30_000);

      // Stale numbers are a smaller lie than an empty page.
      expect(screen.getByText('runner-a')).toBeInTheDocument();
      expect(screen.queryByText('gateway down')).not.toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
  });
});

describe('formatting', () => {
  it('reports poll age in the largest unit that stays readable', () => {
    expect(formatAge(12)).toBe('12s ago');
    expect(formatAge(120)).toBe('2m ago');
    expect(formatAge(7200)).toBe('2h ago');
    expect(formatAge(172800)).toBe('2d ago');
  });

  it('omits the ceiling for a host that declares none', () => {
    expect(formatSlots({ slots_used: 2, slots_total: 4 } as never)).toBe('2 / 4');
    expect(formatSlots({ slots_used: 2, slots_total: null } as never)).toBe('2');
  });
});

describe('harness version selection', () => {
  beforeEach(() => {
    auth.role = 'admin';
    Object.values(mocked).forEach((m) => m.mockReset());
    mocked.list.mockResolvedValue(pool([host()]));
  });

  afterEach(cleanup);

  it('offers every version the pool holds, with the image default first', async () => {
    render(<AdminHostsPage />);

    const select = (await screen.findByLabelText('claude-code')) as HTMLSelectElement;
    expect([...select.options].map((o) => o.textContent)).toEqual([
      'Image default',
      '2.1.220',
      '2.1.235',
    ]);
    // Nothing selected means the image default — not "2.1.220 happens to be
    // first". The two differ the moment a host is rebuilt.
    expect(select.value).toBe('');
  });

  it('shows the deployment-wide selection, not a per-host value', async () => {
    mocked.list.mockResolvedValue(pool([host()], null, { 'claude-code': '2.1.235' }));
    render(<AdminHostsPage />);

    const select = (await screen.findByLabelText('claude-code')) as HTMLSelectElement;
    await waitFor(() => expect(select.value).toBe('2.1.235'));
  });

  it('sends the choice and takes the whole pool back', async () => {
    mocked.setVersion.mockResolvedValue(pool([host()], null, { 'claude-code': '2.1.235' }));
    render(<AdminHostsPage />);

    const select = await screen.findByLabelText('claude-code');
    fireEvent.change(select, { target: { value: '2.1.235' } });

    await waitFor(() => expect(mocked.setVersion).toHaveBeenCalledWith('claude-code', '2.1.235'));
    // The wording is the contract with the operator: this is not retroactive,
    // and a page that implied otherwise would be the bug.
    expect(
      await screen.findByText(/New claude-code threads will run 2\.1\.235/),
    ).toBeInTheDocument();
  });

  it('clears back to the image default with null, not an empty string', async () => {
    mocked.list.mockResolvedValue(pool([host()], null, { 'claude-code': '2.1.235' }));
    mocked.setVersion.mockResolvedValue(pool([host()]));
    render(<AdminHostsPage />);

    const select = await screen.findByLabelText('claude-code');
    fireEvent.change(select, { target: { value: '' } });

    await waitFor(() => expect(mocked.setVersion).toHaveBeenCalledWith('claude-code', null));
  });

  it('hides the selector when no host reports a menu', async () => {
    // A pool of pre-menu hosts. An empty control that cannot do anything is
    // worse than no control: it reads as broken rather than as absent.
    mocked.list.mockResolvedValue(pool([host({ runtime_versions_available: null })]));
    render(<AdminHostsPage />);

    await screen.findByText('runner-a');
    expect(screen.queryByText('Harness version')).not.toBeInTheDocument();
  });
});

describe('version and image cells', () => {
  it('lists each runtime default, and renders a host that reports none', () => {
    expect(formatVersions(host() as never)).toBe('claude-code 2.1.220');
    expect(formatVersions(host({ runtime_versions: null }) as never)).toBe('—');
    expect(formatVersions(host({ runtime_versions: {} }) as never)).toBe('—');
  });

  it('shortens a digest to something comparable at a glance', () => {
    expect(formatImage(host() as never)).toBe('abcdef012345');
    // A locally built image has no RepoDigest; its bare id still identifies it.
    expect(formatImage(host({ sandbox_image_digest: 'sha256:0123456789abcdef' }) as never)).toBe(
      '0123456789ab',
    );
    expect(formatImage(host({ sandbox_image_digest: null }) as never)).toBe('—');
  });

  it('unions menus across hosts without losing the default ordering', () => {
    const menu = runtimeMenu([
      host({ runtime_versions_available: { 'claude-code': ['2.1.220'] } }),
      host({
        runtime_versions_available: { 'claude-code': ['2.1.220', '2.1.235'], pi: ['0.73.1'] },
      }),
    ] as never);
    expect(menu).toEqual([
      ['claude-code', ['2.1.220', '2.1.235']],
      ['pi', ['0.73.1']],
    ]);
  });
});
