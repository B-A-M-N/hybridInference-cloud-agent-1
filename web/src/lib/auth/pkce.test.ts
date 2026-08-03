// @vitest-environment jsdom

// Parking and redeeming PKCE verifiers.
//
// The property that needs its own file is concurrency. One fixed storage key
// made a second sign-in overwrite the first one's verifier, so whichever tab
// came back first redeemed its code against the wrong secret and the gateway
// refused it — a login that only fails when the user has two tabs open, and
// only in the half of the flow nobody re-tests by hand.

import { beforeEach, describe, expect, it, vi } from 'vitest';

import { hasPending, rememberPending, takePending, type PkcePending } from './pkce';

function flow(state: string, overrides: Partial<PkcePending> = {}): PkcePending {
  return {
    verifier: `verifier-for-${state}`,
    state,
    returnTo: `/${state}`,
    createdAt: Date.now(),
    ...overrides,
  };
}

describe('pkce pending flows', () => {
  beforeEach(() => {
    localStorage.clear();
    vi.useRealTimers();
  });

  it('keeps concurrent sign-ins apart', () => {
    rememberPending(flow('first'));
    rememberPending(flow('second'));

    expect(takePending('first')?.verifier).toBe('verifier-for-first');
    expect(takePending('second')?.verifier).toBe('verifier-for-second');
  });

  it('consumes only the flow it was asked for', () => {
    rememberPending(flow('first'));
    rememberPending(flow('second'));

    takePending('first');

    expect(takePending('second')).not.toBeNull();
  });

  it('spends a verifier exactly once', () => {
    rememberPending(flow('first'));

    expect(takePending('first')).not.toBeNull();
    expect(takePending('first')).toBeNull();
  });

  it('has nothing for a state it never parked', () => {
    rememberPending(flow('first'));

    expect(takePending('never-started')).toBeNull();
    // …and the miss did not consume the flow that is genuinely in progress.
    expect(takePending('first')).not.toBeNull();
  });

  it('reports whether any flow is still redeemable', () => {
    expect(hasPending()).toBe(false);

    rememberPending(flow('first'));
    expect(hasPending()).toBe(true);

    takePending('first');
    expect(hasPending()).toBe(false);
  });

  it('ignores a verifier older than ten minutes', () => {
    rememberPending(flow('stale', { createdAt: Date.now() - 11 * 60 * 1000 }));

    expect(takePending('stale')).toBeNull();
    expect(hasPending()).toBe(false);
  });

  it('sweeps unredeemable flows when a new one starts, and only those', () => {
    rememberPending(flow('abandoned', { createdAt: Date.now() - 11 * 60 * 1000 }));
    rememberPending(flow('still-running'));

    rememberPending(flow('fresh'));

    // One key per flow only pays for itself if the flows nobody finishes are
    // cleaned up: a redirect the user closes leaves an entry behind forever
    // otherwise. But the sweep is by redeemability, not by abandonment — a
    // still-young entry may be a sign-in running in another tab, and this
    // module cannot tell the two apart.
    expect(localStorage.getItem('cloud-agent:pkce:abandoned')).toBeNull();
    expect(localStorage.getItem('cloud-agent:pkce:still-running')).not.toBeNull();
    expect(localStorage.getItem('cloud-agent:pkce:fresh')).not.toBeNull();
  });

  it('refuses an entry whose contents do not match its key', () => {
    localStorage.setItem(
      'cloud-agent:pkce:first',
      JSON.stringify(flow('second')), // written by something that is not this module
    );

    expect(takePending('first')).toBeNull();
  });
});
