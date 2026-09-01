'use client';

// Recreated. The source repo's version pushed unauthenticated visitors at its
// own `/login` page; this service has no login page and no password to check.
// An unauthenticated visitor is handed to the control plane, which runs the
// whole round trip to the gateway's consent page and brings them back signed
// in — no PKCE, no code, and no callback route in this app.

import { useEffect, useRef, useState } from 'react';
import { useAuth } from '@/components/providers';
import { beginSignIn, SignInNotConfigured } from '@/lib/auth/signin';

/**
 * Set by the control plane when it cannot sign anyone in.
 *
 * **This is what keeps a misconfigured deployment from looping.** The browser
 * cannot see the server's configuration, so the only way it learns that
 * sign-in is impossible is being told on the way back — and if it asked again
 * it would be told again, forever.
 */
const SIGN_IN_ERROR_PARAM = 'sign_in_error';

export function ProtectedRoute({ children }: { children: React.ReactNode }) {
  const { state } = useAuth();
  const [error, setError] = useState<string | null>(null);
  // One redirect per mount. Without this a slow navigation lets the effect run
  // twice and mint a second verifier, invalidating the first.
  const started = useRef(false);

  useEffect(() => {
    if (state.loading || state.isAuthenticated || started.current) return;
    started.current = true;
    // Read from `location` rather than `useSearchParams`, which would force
    // every page under this layout behind a Suspense boundary at build time.
    // The effect is client-only, so `location` is always the current route.
    if (new URLSearchParams(window.location.search).has(SIGN_IN_ERROR_PARAM)) {
      setError('Sign-in is not configured for this deployment.');
      return;
    }
    const returnTo = `${window.location.pathname}${window.location.search}`;
    beginSignIn(returnTo).catch((err) => {
      started.current = false;
      setError(
        err instanceof SignInNotConfigured
          ? 'Sign-in is not configured for this deployment.'
          : 'Could not start sign-in. Please try again.',
      );
    });
  }, [state.loading, state.isAuthenticated]);

  if (error) {
    return (
      <div className="flex min-h-screen flex-col items-center justify-center gap-2">
        <h1 className="text-xl font-semibold text-gray-900 dark:text-gray-50">
          Sign-in unavailable
        </h1>
        <p className="text-sm text-gray-500 dark:text-gray-400" role="alert">
          {error}
        </p>
      </div>
    );
  }

  if (state.loading || !state.isAuthenticated) {
    return (
      <div className="flex items-center justify-center min-h-screen">
        <div className="h-12 w-12 animate-spin rounded-full border-4 border-gray-300 dark:border-white/15 border-t-blue-600"></div>
      </div>
    );
  }

  return <>{children}</>;
}
