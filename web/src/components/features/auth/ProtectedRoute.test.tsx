// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

// F3's acceptance, both halves: an unauthenticated visitor is sent to the
// gateway with a PKCE challenge, and an authenticated one sees the app.
//
// The session BFF is mocked rather than served — what is being tested is the
// browser's half of the contract, and a real control plane would only add a
// second thing that can be wrong.

import { cleanup, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('@/config/env', () => ({
  agentApiBase: '',
  gatewayAuthorizeUrl: 'https://gateway.example.org/authorize',
  gatewayTokenUrl: () => 'https://gateway.example.org/v1/identity/token',
  OAUTH_CLIENT_ID: 'cloud-agent',
  config: { apiBase: '', appName: 'Cloud Agent' },
}));

vi.mock('@/lib/api/session', () => ({
  getSessionUser: vi.fn(),
  endSession: vi.fn(),
  startSession: vi.fn(),
  exchangeAuthorizationCode: vi.fn(),
}));

import { getSessionUser } from '@/lib/api/session';
import { AuthProvider } from '@/components/providers/AuthProvider';
import { ProtectedRoute } from './ProtectedRoute';

const assign = vi.fn();

function stubLocation(pathname = '/task-1', search = '?tab=diff') {
  Object.defineProperty(window, 'location', {
    configurable: true,
    writable: true,
    value: { origin: 'https://agents.example.org', pathname, search, assign },
  });
}

function renderApp() {
  return render(
    <AuthProvider>
      <ProtectedRoute>
        <p>task list</p>
      </ProtectedRoute>
    </AuthProvider>,
  );
}

describe('ProtectedRoute', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
    stubLocation();
  });
  afterEach(() => cleanup());

  it('sends an unauthenticated visitor to the gateway with a PKCE challenge', async () => {
    vi.mocked(getSessionUser).mockResolvedValue(null);

    renderApp();

    await waitFor(() => expect(assign).toHaveBeenCalledOnce());
    const url = new URL(assign.mock.calls[0][0] as string);

    expect(url.origin + url.pathname).toBe('https://gateway.example.org/authorize');
    expect(url.searchParams.get('client_id')).toBe('cloud-agent');
    expect(url.searchParams.get('redirect_uri')).toBe('https://agents.example.org/callback');
    expect(url.searchParams.get('code_challenge_method')).toBe('S256');
    // 32 bytes of SHA-256, base64url, unpadded.
    expect(url.searchParams.get('code_challenge')).toMatch(/^[A-Za-z0-9_-]{43}$/);
    expect(url.searchParams.get('state')).toBeTruthy();

    expect(screen.queryByText('task list')).not.toBeInTheDocument();
  });

  it('parks a verifier that matches the challenge it sent, and the page to return to', async () => {
    vi.mocked(getSessionUser).mockResolvedValue(null);

    renderApp();

    await waitFor(() => expect(assign).toHaveBeenCalledOnce());
    const url = new URL(assign.mock.calls[0][0] as string);
    const pending = JSON.parse(localStorage.getItem('cloud-agent:pkce') ?? '{}');

    expect(pending.state).toBe(url.searchParams.get('state'));
    expect(pending.returnTo).toBe('/task-1?tab=diff');
    // The challenge really is S256(verifier) — a mismatch here is a login loop
    // that only shows up against a live gateway.
    const digest = await crypto.subtle.digest(
      'SHA-256',
      new TextEncoder().encode(pending.verifier),
    );
    const expected = btoa(String.fromCharCode(...new Uint8Array(digest)))
      .replace(/\+/g, '-')
      .replace(/\//g, '_')
      .replace(/=+$/, '');
    expect(url.searchParams.get('code_challenge')).toBe(expected);
  });

  it('renders the app for a signed-in user and redirects nobody', async () => {
    vi.mocked(getSessionUser).mockResolvedValue({
      id: 'u1',
      email: 'ada@example.com',
      role: 'internal',
    });

    renderApp();

    expect(await screen.findByText('task list')).toBeInTheDocument();
    expect(assign).not.toHaveBeenCalled();
  });

  it('refuses to sign in rather than loop when no gateway is configured', async () => {
    vi.mocked(getSessionUser).mockResolvedValue(null);
    const env = await import('@/config/env');
    vi.spyOn(env, 'gatewayAuthorizeUrl', 'get').mockReturnValue('');

    renderApp();

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Sign-in is not configured for this deployment.',
    );
    expect(assign).not.toHaveBeenCalled();
  });
});
