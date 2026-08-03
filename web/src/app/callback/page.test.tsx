// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

// The return leg of sign-in, against a mocked gateway and a mocked BFF.
//
// The properties worth pinning are the refusals: a callback whose `state` does
// not match the request that started it, and one arriving with no parked
// verifier at all, must both stop before anything is spent.

import { cleanup, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const searchParams = { current: new URLSearchParams() };
const replace = vi.fn();

vi.mock('next/navigation', () => ({
  useRouter: () => ({ replace, push: vi.fn() }),
  useSearchParams: () => searchParams.current,
}));

vi.mock('next/link', () => ({
  default: ({ href, children }: { href: string; children: React.ReactNode }) => (
    <a href={href}>{children}</a>
  ),
}));

const refreshUser = vi.fn().mockResolvedValue(undefined);
vi.mock('@/components/providers', () => ({
  useAuth: () => ({ refreshUser, state: { loading: false, isAuthenticated: false, user: null } }),
}));

vi.mock('@/config/env', () => ({
  agentApiBase: '',
  gatewayAuthorizeUrl: 'https://gateway.example.org/authorize',
  gatewayTokenUrl: () => 'https://gateway.example.org/v1/identity/token',
  OAUTH_CLIENT_ID: 'cloud-agent',
  config: { apiBase: '', appName: 'Cloud Agent' },
}));

vi.mock('@/lib/api/session', () => ({
  exchangeAuthorizationCode: vi.fn(),
  startSession: vi.fn(),
  getSessionUser: vi.fn(),
  endSession: vi.fn(),
}));

import { exchangeAuthorizationCode, startSession } from '@/lib/api/session';
import CallbackPage from './page';

const PENDING = {
  verifier: 'v'.repeat(43),
  state: 'st4te',
  returnTo: '/task-1',
  createdAt: Date.now(),
};

function park(overrides: Partial<typeof PENDING> = {}) {
  localStorage.setItem('cloud-agent:pkce', JSON.stringify({ ...PENDING, ...overrides }));
}

describe('sign-in callback', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
    searchParams.current = new URLSearchParams('code=abc&state=st4te');
    Object.defineProperty(window, 'location', {
      configurable: true,
      writable: true,
      value: { origin: 'https://agents.example.org', pathname: '/callback', search: '' },
    });
    vi.mocked(exchangeAuthorizationCode).mockResolvedValue('identity-token');
    vi.mocked(startSession).mockResolvedValue({ id: 'u1', email: null, role: 'internal' });
  });
  afterEach(() => cleanup());

  it('exchanges the code, opens a session, and returns where sign-in started', async () => {
    park();

    render(<CallbackPage />);

    await waitFor(() => expect(replace).toHaveBeenCalledWith('/task-1'));
    expect(exchangeAuthorizationCode).toHaveBeenCalledWith({
      code: 'abc',
      verifier: PENDING.verifier,
      redirectUri: 'https://agents.example.org/callback',
    });
    expect(startSession).toHaveBeenCalledWith('identity-token');
    expect(refreshUser).toHaveBeenCalledOnce();
  });

  it('spends the verifier exactly once', async () => {
    park();

    render(<CallbackPage />);

    await waitFor(() => expect(replace).toHaveBeenCalled());
    expect(localStorage.getItem('cloud-agent:pkce')).toBeNull();
  });

  it('refuses a response whose state does not match the request', async () => {
    park({ state: 'a-different-request' });

    render(<CallbackPage />);

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'did not match the request that started it',
    );
    expect(exchangeAuthorizationCode).not.toHaveBeenCalled();
    expect(replace).not.toHaveBeenCalled();
  });

  it('refuses a callback with nothing parked to redeem it with', async () => {
    render(<CallbackPage />);

    expect(await screen.findByRole('alert')).toHaveTextContent('expired');
    expect(exchangeAuthorizationCode).not.toHaveBeenCalled();
  });

  it('refuses a callback that carries no code', async () => {
    park();
    searchParams.current = new URLSearchParams('state=st4te');

    render(<CallbackPage />);

    expect(await screen.findByRole('alert')).toHaveTextContent('did not return an authorization');
    expect(startSession).not.toHaveBeenCalled();
  });

  it('reports a refused exchange instead of leaving a spinner up', async () => {
    park();
    vi.mocked(exchangeAuthorizationCode).mockRejectedValue(new Error('invalid_grant'));

    render(<CallbackPage />);

    expect(await screen.findByRole('alert')).toHaveTextContent('invalid_grant');
    expect(startSession).not.toHaveBeenCalled();
    expect(replace).not.toHaveBeenCalled();
  });
});
