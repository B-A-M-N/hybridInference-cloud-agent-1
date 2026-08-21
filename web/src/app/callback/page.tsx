'use client';

// The second half of sign-in.
//
// The gateway has sent the browser back with a one-time authorization code. We
// redeem it there for an identity token, hand that token straight to our own
// control plane in exchange for a session cookie, and forget it. The window in
// which this app holds a gateway credential is one function call wide.
//
// Order matters and is deliberate:
//   1. The returned `state` is what the parked flow is looked up by, so a
//      callback we did not initiate finds nothing and is refused before
//      anything is spent.
//   2. The pending record is consumed on read, so a code that fails to
//      exchange cannot be retried against the same verifier. Only this flow's
//      record is consumed — a sign-in running in another tab is untouched.
//   3. Only after the session call succeeds does the app navigate anywhere.

import { Suspense, useEffect, useRef, useState } from 'react';
import Link from 'next/link';
import { useRouter, useSearchParams } from 'next/navigation';
import { useAuth } from '@/components/providers';
import { exchangeAuthorizationCode, startSession } from '@/lib/api/session';
import { callbackUri, safeReturnTo } from '@/lib/auth/signin';
import { hasPending, takePending } from '@/lib/auth/pkce';
import { getErrorMessage } from '@/lib/utils/errors';

function CallbackContent() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const { refreshUser } = useAuth();
  const [error, setError] = useState<string | null>(null);
  const ran = useRef(false);

  useEffect(() => {
    if (ran.current) return;
    ran.current = true;

    const code = searchParams?.get('code') ?? '';
    const state = searchParams?.get('state') ?? '';
    // The lookup *is* the state check: flows are parked under their own state,
    // so a response naming a state we did not start finds nothing.
    const pending = takePending(state);
    // Read before anything can be thrown, and only when the lookup missed:
    // another flow still parked means this response belongs to a request we
    // did not make, which is a different thing to tell the user than "nothing
    // is parked at all".
    const another = pending === null && hasPending();

    async function finish() {
      if (!pending) {
        throw new Error(
          another
            ? 'This sign-in response did not match the request that started it.'
            : 'This sign-in link has expired. Please start again.',
        );
      }
      if (!code) {
        throw new Error('The gateway did not return an authorization code.');
      }
      const identityToken = await exchangeAuthorizationCode({
        code,
        verifier: pending.verifier,
        redirectUri: callbackUri(),
      });
      await startSession(identityToken);
      await refreshUser();
      router.replace(safeReturnTo(pending.returnTo));
    }

    finish().catch((err) => setError(getErrorMessage(err)));
  }, [searchParams, refreshUser, router]);

  if (error) {
    return (
      <div className="flex min-h-screen flex-col items-center justify-center gap-3">
        <h1 className="text-xl font-semibold text-gray-900 dark:text-gray-50">Sign-in failed</h1>
        <p className="text-sm text-gray-500 dark:text-gray-400" role="alert">
          {error}
        </p>
        <Link
          href="/"
          className="text-sm font-medium text-crimson dark:text-crimson-light hover:underline"
        >
          Try again
        </Link>
      </div>
    );
  }

  return (
    <div className="flex min-h-screen items-center justify-center">
      <div className="h-12 w-12 animate-spin rounded-full border-4 border-gray-300 dark:border-white/15 border-t-blue-600"></div>
    </div>
  );
}

export default function CallbackPage() {
  return (
    <Suspense fallback={<div className="min-h-screen bg-gray-50 dark:bg-gray-950" />}>
      <CallbackContent />
    </Suspense>
  );
}
