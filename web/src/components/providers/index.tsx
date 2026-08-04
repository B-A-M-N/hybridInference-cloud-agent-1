'use client';

// Recreated from the source repo's provider stack, minus `QueryProvider`: no
// moved file uses TanStack Query, so shipping it would be a dependency with no
// caller.

import { AuthProvider } from './AuthProvider';
import { ToastProvider } from './ToastProvider';
import { SiteConfigProvider } from './SiteConfigProvider';

export function Providers({ children }: { children: React.ReactNode }) {
  return (
    <SiteConfigProvider>
      <AuthProvider>
        {children}
        <ToastProvider />
      </AuthProvider>
    </SiteConfigProvider>
  );
}

export { useAuth } from './AuthProvider';
