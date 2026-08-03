// Starting the gateway sign-in round trip.
//
// Every security decision belongs to the gateway: which clients it federates
// to, which redirect URIs it accepts (exact match — the callback below must be
// listed verbatim in `IDENTITY_ALLOWED_REDIRECTS`), how long a code lives. All
// this side does is generate the PKCE pair, park the verifier, and navigate.

import { gatewayAuthorizeUrl, OAUTH_CLIENT_ID } from '@/config/env';
import { challengeFor, createState, createVerifier, rememberPending } from './pkce';

/** The one path the gateway is allowed to send the browser back to. */
export const CALLBACK_PATH = '/callback';

export function callbackUri(): string {
  return `${window.location.origin}${CALLBACK_PATH}`;
}

/** Thrown when this deployment has no gateway to sign in against. */
export class SignInNotConfigured extends Error {}

/**
 * Build the gateway's `/authorize` URL for a fresh PKCE flow, remembering the
 * verifier so the callback can redeem the code.
 *
 * Returns the URL rather than navigating so it can be asserted on in a test —
 * the challenge being present is the property that matters, and a function
 * that only ever calls `location.assign` has nothing to check.
 */
export async function buildAuthorizeUrl(returnTo: string): Promise<string> {
  if (!gatewayAuthorizeUrl) {
    throw new SignInNotConfigured(
      'NEXT_PUBLIC_GATEWAY_AUTHORIZE_URL is not set, so this deployment cannot sign anyone in.',
    );
  }
  const verifier = createVerifier();
  const state = createState();
  const redirectUri = callbackUri();

  rememberPending({ verifier, state, returnTo, createdAt: Date.now() });

  const url = new URL(gatewayAuthorizeUrl);
  url.searchParams.set('client_id', OAUTH_CLIENT_ID);
  url.searchParams.set('redirect_uri', redirectUri);
  url.searchParams.set('code_challenge', await challengeFor(verifier));
  url.searchParams.set('code_challenge_method', 'S256');
  url.searchParams.set('state', state);
  return url.toString();
}

/** Send the browser to the gateway's consent page. */
export async function beginSignIn(returnTo: string): Promise<void> {
  const url = await buildAuthorizeUrl(returnTo);
  // A full navigation, not a router push: the destination is another origin.
  window.location.assign(url);
}
