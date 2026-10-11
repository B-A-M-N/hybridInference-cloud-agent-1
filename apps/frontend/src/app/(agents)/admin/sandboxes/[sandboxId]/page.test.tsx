// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

vi.mock('@/lib/api/adminSettings', () => ({
  getAgentAdminSettings: async () => ({
    revision: 1,
    definitions: [],
    values: {},
    secret_configured: {},
    bootstrap: [],
    setup: { required: false, groups: [], bootstrap_missing: [] },
  }),
}));

import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  deleteAdminSandbox,
  getAdminSandboxTrajectory,
  type AdminSandbox,
  type AdminSandboxDetail,
  type AdminSandboxTrajectory,
  type AdminSandboxTrajectoryEvent,
} from '@/lib/api/adminSandboxes';
import type { AgentAccessApi } from '@/lib/api/session';
import { APIError } from '@/lib/utils/errors';

const auth = vi.hoisted(() => ({
  id: 'admin-one',
  access: undefined as AgentAccessApi | undefined,
}));
const navigation = vi.hoisted(() => ({
  sandboxId: 'sandbox/one',
}));

vi.mock('@/components/providers', () => ({
  useAuth: () => ({ state: { user: { id: auth.id, agent_access: auth.access } } }),
}));
vi.mock('next/navigation', () => ({
  useParams: () => ({ sandboxId: navigation.sandboxId }),
}));
vi.mock('@/lib/api/adminSandboxes', () => ({
  deleteAdminSandbox: vi.fn(),
  getAdminSandboxTrajectory: vi.fn(),
}));

import AdminSandboxTrajectoryPage from './page';

const observedAt = '2026-10-10T10:00:00Z';

function sandbox(overrides: Partial<AdminSandbox> = {}): AdminSandbox {
  return {
    id: 'sandbox/one',
    origin_thread_id: 'thread/one',
    title: 'Fix login',
    repo: 'org/repo',
    user_id: 'owner-one',
    host: 'host-one',
    container_id: 'container-one',
    container_name: 'agent-sandbox-one',
    runtime_id: 'codex',
    runtime_version: '1.2.3',
    execution_profile: 'trusted-v1',
    state: 'stopped',
    generation: 4,
    stop_requested_at: observedAt,
    delete_requested_at: null,
    deleted_at: null,
    delete_requested_by: null,
    memory_limit: '4g',
    cpu_limit: '2',
    member_count: 1,
    active_attempt_count: 0,
    created_at: '2026-10-10T09:00:00Z',
    last_activity_at: '2026-10-10T09:59:00Z',
    idle_deadline: null,
    updated_at: observedAt,
    host_last_heartbeat_at: observedAt,
    ...overrides,
  };
}

function event(
  id: number,
  eventType: string,
  createdAt: string,
  overrides: Partial<AdminSandboxTrajectoryEvent> = {},
): AdminSandboxTrajectoryEvent {
  return {
    id,
    generation: 4,
    event_type: eventType,
    from_state: null,
    to_state: null,
    source: 'system',
    actor_id: null,
    created_at: createdAt,
    details: {},
    ...overrides,
  };
}

function trajectory(overrides: Partial<AdminSandboxTrajectory> = {}): AdminSandboxTrajectory {
  return {
    sandbox: sandbox(),
    members: [
      {
        thread_id: 'thread/one',
        title: 'Main session',
        user_id: 'owner-one',
        resident_slot_id: 0,
        resident_state: 'stopped',
        context_status: 'ready',
        native_state: 'ready',
        active_attempt_id: null,
        last_settled_job_id: 'job-one',
        created_at: '2026-10-10T09:00:00Z',
        updated_at: observedAt,
      },
    ],
    member_history_count: 1,
    member_history_truncated: false,
    events: [
      event(2, 'sandbox.stopped', '2026-10-10T09:02:00Z', {
        from_state: 'running',
        to_state: 'stopped',
        source: 'host',
      }),
      event(1, 'sandbox.created', '2026-10-10T09:00:00Z', {
        to_state: 'provisioning',
        details: { image_digest: 'sha256:abc' },
      }),
    ],
    observed_at: observedAt,
    next_cursor: null,
    ...overrides,
  };
}

function pendingDetail(): AdminSandboxDetail {
  return {
    sandbox: sandbox({
      delete_requested_at: '2026-10-10T10:01:00Z',
      delete_requested_by: 'admin-one',
    }),
    members: trajectory().members,
    observed_at: '2026-10-10T10:01:00Z',
  };
}

beforeEach(() => {
  vi.resetAllMocks();
  auth.id = 'admin-one';
  auth.access = { allowed: true, permissions: ['agent.use', 'agent.admin'] };
  navigation.sandboxId = 'sandbox/one';
  vi.mocked(getAdminSandboxTrajectory).mockResolvedValue(trajectory());
  vi.mocked(deleteAdminSandbox).mockResolvedValue(pendingDetail());
  vi.spyOn(window, 'confirm').mockReturnValue(true);
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.useRealTimers();
});

describe('admin sandbox trajectory', () => {
  it.each<AgentAccessApi | undefined>([
    undefined,
    { allowed: false, permissions: ['agent.use', 'agent.admin'] },
    { allowed: true, permissions: ['agent.use'] },
  ])('does not read cross-user history without current admin access: %j', (access) => {
    auth.access = access;
    render(<AdminSandboxTrajectoryPage />);
    expect(screen.getByRole('heading', { name: 'Admin access required' })).toBeInTheDocument();
    expect(getAdminSandboxTrajectory).not.toHaveBeenCalled();
  });

  it('renders lifecycle events chronologically with states, sources, timestamps, and details', async () => {
    render(<AdminSandboxTrajectoryPage />);

    expect(await screen.findByRole('heading', { name: 'Fix login' })).toBeInTheDocument();
    expect(getAdminSandboxTrajectory).toHaveBeenCalledWith(
      'sandbox/one',
      { limit: 200 },
      expect.any(AbortSignal),
    );
    expect(screen.getByRole('link', { name: 'Main session' })).toHaveAttribute(
      'href',
      '/admin/sessions/thread%2Fone',
    );

    const list = screen.getByRole('list', { name: 'Sandbox trajectory' });
    const rows = within(list).getAllByRole('listitem');
    expect(rows).toHaveLength(2);
    expect(rows[0]).toHaveTextContent('Sandbox created');
    expect(rows[0]).toHaveTextContent('unknown → provisioning');
    expect(rows[0]).toHaveTextContent('Source: system');
    expect(rows[0]).toHaveTextContent('Image digest');
    expect(rows[0]).toHaveTextContent('sha256:abc');
    expect(rows[1]).toHaveTextContent('Sandbox stopped');
    expect(rows[1]).toHaveTextContent('running → stopped');
    expect(rows[1]).toHaveTextContent('Source: host');
    expect(rows[0].querySelector('time')).toHaveTextContent('Oct 10, 2026, 9:00:00 AM UTC');
  });

  it('shows when the bounded member history omits older rows', async () => {
    vi.mocked(getAdminSandboxTrajectory).mockResolvedValue(
      trajectory({
        member_history_count: 300,
        member_history_truncated: true,
      }),
    );

    render(<AdminSandboxTrajectoryPage />);

    expect(
      await screen.findByRole('heading', { name: 'Member sessions (300)' }),
    ).toBeInTheDocument();
    expect(screen.getByRole('status')).toHaveTextContent(
      'Showing 1 of 300 historical member sessions',
    );
  });

  it('renders a deleted tombstone and its final lifecycle event on direct access', async () => {
    vi.mocked(getAdminSandboxTrajectory).mockResolvedValue(
      trajectory({
        sandbox: sandbox({
          container_id: null,
          delete_requested_at: '2026-10-10T10:01:00Z',
          delete_requested_by: 'admin-one',
          deleted_at: '2026-10-10T10:02:00Z',
        }),
        events: [
          event(3, 'deleted', '2026-10-10T10:02:00Z', {
            from_state: 'stopped',
            to_state: 'deleted',
            source: 'host',
          }),
        ],
      }),
    );
    render(<AdminSandboxTrajectoryPage />);

    expect(await screen.findByText(/Deleted Oct 10, 2026, 10:02:00 AM UTC/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Sandbox deleted' })).toBeDisabled();
    const timeline = screen.getByRole('list', { name: 'Sandbox trajectory' });
    expect(within(timeline).getByText('Deleted')).toBeInTheDocument();
  });
  it('loads and merges another trajectory page without duplicating events', async () => {
    vi.mocked(getAdminSandboxTrajectory)
      .mockResolvedValueOnce(trajectory({ next_cursor: 'next+page' }))
      .mockResolvedValueOnce(
        trajectory({
          events: [
            event(1, 'sandbox.created', '2026-10-10T09:00:00Z'),
            event(3, 'sandbox.started', '2026-10-10T09:01:00Z', {
              from_state: 'starting',
              to_state: 'running',
            }),
          ],
          next_cursor: null,
        }),
      );
    render(<AdminSandboxTrajectoryPage />);

    fireEvent.click(await screen.findByRole('button', { name: 'Load more events' }));
    await waitFor(() => expect(getAdminSandboxTrajectory).toHaveBeenCalledTimes(2));
    expect(getAdminSandboxTrajectory).toHaveBeenLastCalledWith(
      'sandbox/one',
      { limit: 200, cursor: 'next+page' },
      expect.any(AbortSignal),
    );

    const rows = within(screen.getByRole('list', { name: 'Sandbox trajectory' })).getAllByRole(
      'listitem',
    );
    expect(rows).toHaveLength(3);
    expect(rows.map((row) => row.textContent)).toEqual([
      expect.stringContaining('Sandbox created'),
      expect.stringContaining('Sandbox started'),
      expect.stringContaining('Sandbox stopped'),
    ]);
    expect(screen.queryByRole('button', { name: 'Load more events' })).not.toBeInTheDocument();
  });

  it('does not let stale pagination overwrite a newer deletion tombstone', async () => {
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] });
    let resolveOlder!: (value: AdminSandboxTrajectory) => void;
    const olderPage = new Promise<AdminSandboxTrajectory>((resolve) => {
      resolveOlder = resolve;
    });
    const pending = trajectory({
      sandbox: sandbox({
        delete_requested_at: '2026-10-10T10:01:00Z',
        delete_requested_by: 'admin-one',
      }),
      next_cursor: 'older-events',
    });
    vi.mocked(getAdminSandboxTrajectory)
      .mockResolvedValueOnce(pending)
      .mockReturnValueOnce(olderPage)
      .mockResolvedValueOnce(
        trajectory({
          sandbox: sandbox({
            container_id: null,
            delete_requested_at: '2026-10-10T10:01:00Z',
            delete_requested_by: 'admin-one',
            deleted_at: '2026-10-10T10:02:00Z',
          }),
          events: [
            event(3, 'deleted', '2026-10-10T10:02:00Z', {
              from_state: 'stopped',
              to_state: 'deleted',
              source: 'host',
            }),
          ],
        }),
      );
    render(<AdminSandboxTrajectoryPage />);

    fireEvent.click(await screen.findByRole('button', { name: 'Load more events' }));
    await waitFor(() => expect(getAdminSandboxTrajectory).toHaveBeenCalledTimes(2));
    await act(async () => vi.advanceTimersByTimeAsync(3_000));
    expect(await screen.findByText(/Deleted Oct 10, 2026, 10:02:00 AM UTC/)).toBeInTheDocument();

    await act(async () => {
      resolveOlder(
        trajectory({
          sandbox: pending.sandbox,
          events: [event(1, 'sandbox.created', '2026-10-10T09:00:00Z')],
          next_cursor: null,
          observed_at: '2026-10-10T10:01:30Z',
        }),
      );
      await olderPage;
    });

    expect(screen.getByText(/Deleted Oct 10, 2026, 10:02:00 AM UTC/)).toBeInTheDocument();
    expect(screen.queryByText(/Waiting for the host/)).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Sandbox deleted' })).toBeDisabled();
  });

  it('deletes only a stopped sandbox and retains the completed trajectory', async () => {
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] });
    vi.mocked(getAdminSandboxTrajectory)
      .mockResolvedValueOnce(trajectory())
      .mockResolvedValueOnce(
        trajectory({
          sandbox: sandbox({
            delete_requested_at: '2026-10-10T10:01:00Z',
            delete_requested_by: 'admin-one',
            deleted_at: '2026-10-10T10:02:00Z',
          }),
          events: [
            event(3, 'deleted', '2026-10-10T10:02:00Z', {
              from_state: 'stopped',
              to_state: 'deleted',
              source: 'host',
            }),
          ],
        }),
      );
    render(<AdminSandboxTrajectoryPage />);

    fireEvent.click(await screen.findByRole('button', { name: 'Delete sandbox' }));
    expect(window.confirm).toHaveBeenCalledWith(expect.stringContaining('cannot be undone'));
    expect(deleteAdminSandbox).toHaveBeenCalledWith(
      'sandbox/one',
      {
        generation: 4,
        container_id: 'container-one',
        last_activity_at: '2026-10-10T09:59:00Z',
        confirm: true,
      },
      expect.any(AbortSignal),
    );
    expect(
      await screen.findByText(/Deletion requested .*Waiting for the host/),
    ).toBeInTheDocument();
    await act(async () => vi.advanceTimersByTimeAsync(3_000));
    expect(await screen.findByText(/Deleted Oct 10, 2026, 10:02:00 AM UTC/)).toBeInTheDocument();
    expect(screen.getByText(/Lifecycle history remains available/)).toBeInTheDocument();
    expect(screen.getByText('Requested by admin-one.')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Sandbox deleted' })).toBeDisabled();
    const timeline = screen.getByRole('list', { name: 'Sandbox trajectory' });
    expect(within(timeline).getByText('Deleted')).toBeInTheDocument();
  });

  it('does not offer deletion while the sandbox is running', async () => {
    vi.mocked(getAdminSandboxTrajectory).mockResolvedValue(
      trajectory({ sandbox: sandbox({ state: 'running' }) }),
    );
    render(<AdminSandboxTrajectoryPage />);
    expect(await screen.findByRole('button', { name: 'Delete sandbox' })).toBeDisabled();
    expect(screen.getByText('Stop the sandbox before deleting it.')).toBeInTheDocument();
    expect(deleteAdminSandbox).not.toHaveBeenCalled();
  });

  it('clears trajectory data when the server rejects current admin access', async () => {
    vi.mocked(getAdminSandboxTrajectory).mockRejectedValue(
      new APIError('FORBIDDEN', 'Admin access required', 403),
    );
    render(<AdminSandboxTrajectoryPage />);
    expect(
      await screen.findByRole('heading', { name: 'Admin access required' }),
    ).toBeInTheDocument();
    expect(screen.queryByText('owner-one')).not.toBeInTheDocument();
  });
});
