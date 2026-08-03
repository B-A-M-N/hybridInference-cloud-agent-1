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
// `localStorage` — per-origin rather than per-tab, which is also what keeps the
// flow working when the browser comes back into a different tab from the one
// that started it.
//
// **One entry per flow, keyed by its `state`.** A single fixed key made
// concurrent sign-ins destroy each other: a second tab starting a flow
// overwrote the first tab's verifier, so the first callback redeemed its code
// against the wrong one and the gateway refused it — a login that fails only
// when the user has two tabs open. The `state` already uniquely identifies a
// flow and already travels to the gateway and back, so it is the key.
//
// Nothing accumulates: an entry is deleted the moment it is read, entries
// older than `MAX_AGE_MS` are ignored, and starting a new flow sweeps the ones
// that were abandoned.

const STORAGE_PREFIX = 'cloud-agent:pkce:';

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

function storageKey(state: string): string {
  return `${STORAGE_PREFIX}${state}`;
}

/** Every key this module owns, or `[]` when storage is unavailable. */
function pendingKeys(): string[] {
  const keys: string[] = [];
  try {
    for (let index = 0; index < localStorage.length; index += 1) {
      const key = localStorage.key(index);
      if (key !== null && key.startsWith(STORAGE_PREFIX)) keys.push(key);
    }
  } catch {
    return [];
  }
  return keys;
}

/** Parse one stored entry, treating stale or malformed as absent. */
function parsePending(raw: string): PkcePending | null {
  try {
    const parsed = JSON.parse(raw) as PkcePending;
    if (typeof parsed?.verifier !== 'string' || typeof parsed?.state !== 'string') return null;
    if (Date.now() - (parsed.createdAt ?? 0) > MAX_AGE_MS) return null;
    return parsed;
  } catch {
    return null;
  }
}

function readAt(key: string): PkcePending | null {
  try {
    const raw = localStorage.getItem(key);
    return raw === null ? null : parsePending(raw);
  } catch {
    return null;
  }
}

/** Drop entries that can no longer be redeemed, so abandoned flows do not pile up. */
function purgeExpired(): void {
  for (const key of pendingKeys()) {
    if (readAt(key) !== null) continue;
    try {
      localStorage.removeItem(key);
    } catch {
      // Nothing to do: the entry was unreadable anyway.
    }
  }
}

/** Park the pending flow so the callback page can finish it. */
export function rememberPending(pending: PkcePending): void {
  purgeExpired();
  try {
    localStorage.setItem(storageKey(pending.state), JSON.stringify(pending));
  } catch {
    // Blocked storage means the callback will have nothing to redeem with. The
    // callback reports that as a plain failure rather than looping.
  }
}

/**
 * Read and consume the flow that `state` started.
 *
 * Always clears, even when the entry is stale or unparseable: a verifier that
 * survives a failed exchange is a verifier waiting to be replayed. Only the
 * entry for this `state` is touched, so a callback for one flow cannot discard
 * another tab's.
 */
export function takePending(state: string): PkcePending | null {
  let raw: string | null = null;
  try {
    raw = localStorage.getItem(storageKey(state));
    localStorage.removeItem(storageKey(state));
  } catch {
    return null;
  }
  if (raw === null) return null;
  const parsed = parsePending(raw);
  // The key is the state, so this can only differ if something else wrote the
  // entry. Refuse rather than trust it.
  return parsed !== null && parsed.state === state ? parsed : null;
}

/**
 * Whether any redeemable flow is still parked.
 *
 * Lets the callback tell "this response belongs to a request we did not make"
 * apart from "there is nothing here at all" — a distinction the user acts on
 * differently, and one a single-keyed store used to get by comparing the
 * parked `state` against the returned one.
 */
export function hasPending(): boolean {
  return pendingKeys().some((key) => readAt(key) !== null);
}
