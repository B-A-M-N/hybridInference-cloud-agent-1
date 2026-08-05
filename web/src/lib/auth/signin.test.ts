// @vitest-environment jsdom

// The redirect URI must name the path the callback is actually served at.
//
// Mounted under `basePath=/agents`, the callback page lives at
// `/agents/callback`. Next.js applies the prefix to the routes and links it
// generates — not to a string this module concatenates. So a `callbackUri()`
// that forgets the prefix names a path that does not exist, and because the
// gateway matches `IDENTITY_ALLOWED_REDIRECTS` as an exact string, the failure
// arrives as a refusal at `/authorize` before the user reaches consent.
//
// `basePath` is read at module scope from `process.env`, so each case
// re-imports the module with the environment already set. `vi.resetModules()`
// is what makes that re-import real rather than a cache hit.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const ORIGIN = 'https://staging.freeinference.org';

async function loadSignin(basePath: string | undefined) {
  vi.resetModules();
  if (basePath === undefined) delete process.env.NEXT_PUBLIC_BASE_PATH;
  else process.env.NEXT_PUBLIC_BASE_PATH = basePath;
  return import('./signin');
}

describe('callbackUri', () => {
  const original = process.env.NEXT_PUBLIC_BASE_PATH;

  beforeEach(() => {
    Object.defineProperty(window, 'location', {
      value: { origin: ORIGIN, assign: vi.fn() },
      writable: true,
    });
  });

  afterEach(() => {
    if (original === undefined) delete process.env.NEXT_PUBLIC_BASE_PATH;
    else process.env.NEXT_PUBLIC_BASE_PATH = original;
    vi.resetModules();
  });

  it('carries the base path the app is mounted under', async () => {
    const { callbackUri } = await loadSignin('/agents');
    expect(callbackUri()).toBe(`${ORIGIN}/agents/callback`);
  });

  it('omits it entirely for a deployment that owns its hostname', async () => {
    const { callbackUri } = await loadSignin(undefined);
    expect(callbackUri()).toBe(`${ORIGIN}/callback`);
  });

  it('never doubles a slash, whatever the prefix was written as', async () => {
    for (const written of ['/agents/', 'agents', '  /agents  ']) {
      const { callbackUri } = await loadSignin(written);
      expect(callbackUri()).toBe(`${ORIGIN}/agents/callback`);
    }
  });

  it('is what the authorize URL actually sends', async () => {
    process.env.NEXT_PUBLIC_GATEWAY_AUTHORIZE_URL = `${ORIGIN}/authorize`;
    const { buildAuthorizeUrl, callbackUri } = await loadSignin('/agents');

    const url = new URL(await buildAuthorizeUrl('/'));

    // The pair that matters: whatever the deployment must allowlist is
    // exactly what goes on the wire. Asserting only on `callbackUri()` would
    // pass while `buildAuthorizeUrl` sent something else.
    expect(url.searchParams.get('redirect_uri')).toBe(callbackUri());
    expect(url.searchParams.get('redirect_uri')).toBe(`${ORIGIN}/agents/callback`);
  });
});
