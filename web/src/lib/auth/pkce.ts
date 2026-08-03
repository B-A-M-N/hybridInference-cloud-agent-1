// PKCE (RFC 7636) for the gateway sign-in round trip.
//
// This app is a public client: it ships to the browser, so it holds no secret
// and cannot prove its identity with one. PKCE is the substitute. We generate a
// random *verifier*, send only its SHA-256 *challenge* to the gateway, and
// present the verifier when redeeming the code. An attacker who intercepts the
// authorization code cannot spend it without the verifier, which never left
// this origin.
//
// **Where the verifier is parked between the two halves of the flow.** It has
// to survive a full-page navigation to the gateway and back, so it lives in
// `localStorage` under a short expiry and is deleted the moment it is read.
// `sessionStorage` would be the more natural per-tab home, but F2's acceptance
// criterion forbids that API outright — it is how the old repo kept an access
// token, and the grep that enforces its absence cannot tell the two uses apart.
// The trade is small: a PKCE verifier is single-use, worthless without the
// matching code, and gone within a minute either way.

const STORAGE_KEY = 'cloud-agent:pkce';

/** A verifier older than this is abandoned rather than replayed. */
const MAX_AGE_MS = 10 * 60 * 1000;

export interface PkcePending {
  verifier: string;
  state: string;
  /** Where to send the user once sign-in completes. */
  returnTo: string;
  createdAt: number;
}

function base64UrlEncode(bytes: Uint8Array): string {
  let binary = '';
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

function randomBase64Url(byteLength: number): string {
  const bytes = new Uint8Array(byteLength);
  crypto.getRandomValues(bytes);
  return base64UrlEncode(bytes);
}

/** A fresh code verifier: 32 random bytes, base64url — 43 characters. */
export function createVerifier(): string {
  return randomBase64Url(32);
}

/** A fresh `state`, which binds the callback to the request that started it. */
export function createState(): string {
  return randomBase64Url(16);
}

/** S256 challenge for a verifier. */
export async function challengeFor(verifier: string): Promise<string> {
  const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(verifier));
  return base64UrlEncode(new Uint8Array(digest));
}

/** Park the pending flow so the callback page can finish it. */
export function rememberPending(pending: PkcePending): void {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(pending));
  } catch {
    // Blocked storage means the callback will have nothing to redeem with. The
    // callback reports that as a plain failure rather than looping.
  }
}

/**
 * Read and consume the pending flow.
 *
 * Always clears, even when the entry is stale or unparseable: a verifier that
 * survives a failed exchange is a verifier waiting to be replayed.
 */
export function takePending(): PkcePending | null {
  let raw: string | null = null;
  try {
    raw = localStorage.getItem(STORAGE_KEY);
    localStorage.removeItem(STORAGE_KEY);
  } catch {
    return null;
  }
  if (!raw) return null;
  try {
    const parsed = JSON.parse(raw) as PkcePending;
    if (typeof parsed?.verifier !== 'string' || typeof parsed?.state !== 'string') return null;
    if (Date.now() - (parsed.createdAt ?? 0) > MAX_AGE_MS) return null;
    return parsed;
  } catch {
    return null;
  }
}
