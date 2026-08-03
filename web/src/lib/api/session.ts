// The session BFF: this service's own half of sign-in.
//
// The browser never holds the gateway's identity token for longer than one
// call. The callback page exchanges its authorization code at the gateway, and
// immediately hands the resulting token here; the control plane verifies it
// against the gateway's JWKS and replies with an HttpOnly cookie
// (`backend/cloud_agent/identity.py`). From then on the token is spent and the
// cookie is the only credential in play.
//
// These three routes are task **E8**'s to implement. The shapes below are the
// contract this app is written against.

import { agentApiBase, gatewayTokenUrl, OAUTH_CLIENT_ID } from '@/config/env';
import { jsonOrThrow, safeFetch } from './client';
import { APIError } from '@/lib/utils/errors';

export interface SessionUserApi {
  id: string;
  email: string | null;
  role: string;
  user_name?: string | null;
}

/** Who the cookie says we are. A 401 here means "not signed in", not an error. */
export async function getSessionUser(): Promise<SessionUserApi | null> {
  const resp = await safeFetch(`${agentApiBase}/v1/session/me`, {
    credentials: 'include',
    cache: 'no-store',
  });
  if (resp.status === 401 || resp.status === 403) return null;
  return jsonOrThrow<SessionUserApi>(resp);
}

/** Trade a gateway identity token for our session cookie. */
export async function startSession(identityToken: string): Promise<SessionUserApi> {
  const resp = await safeFetch(`${agentApiBase}/v1/session/login`, {
    method: 'POST',
    credentials: 'include',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ identity_token: identityToken }),
  });
  return jsonOrThrow<SessionUserApi>(resp);
}

/** Drop the session cookie. Best-effort: the UI signs out either way. */
export async function endSession(): Promise<void> {
  await safeFetch(`${agentApiBase}/v1/session/logout`, {
    method: 'POST',
    credentials: 'include',
  });
}

/**
 * Redeem an authorization code at the **gateway** for an identity token.
 *
 * This is the one call in the app that does not go to our control plane. It
 * needs no credential — PKCE is what authenticates it — so it deliberately
 * does not send cookies to a foreign origin.
 */
export async function exchangeAuthorizationCode(args: {
  code: string;
  verifier: string;
  redirectUri: string;
}): Promise<string> {
  const tokenUrl = gatewayTokenUrl();
  if (!tokenUrl) {
    throw new APIError('UNKNOWN_ERROR', 'Sign-in is not configured for this deployment.');
  }
  const resp = await safeFetch(tokenUrl, {
    method: 'POST',
    credentials: 'omit',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      code: args.code,
      code_verifier: args.verifier,
      client_id: OAUTH_CLIENT_ID,
      redirect_uri: args.redirectUri,
    }),
  });
  const body = await jsonOrThrow<{ access_token?: string }>(resp);
  if (!body.access_token) {
    throw new APIError('INVALID_TOKEN', 'The gateway returned no identity token.');
  }
  return body.access_token;
}
