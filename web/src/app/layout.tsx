import '../styles/globals.css';
import { config } from '@/config/env';
import { Providers } from '@/components/providers';

// The root document.
//
// The moved agent shell (`(agents)/layout.tsx`) is a *nested* layout: it is the
// same file the source repo mounted under /agents, and it renders a sidebar and
// a header, not an `<html>` document. Rather than graft `<html>`/`<body>` into
// it — an edit to a moved file, and one that would make its own test render a
// document inside a `<div>` — the shell keeps its shape inside the `(agents)`
// route group, and this file provides the document around it. Route groups do
// not appear in URLs, so `/`, `/[jobId]`, `/archived`, `/connected` and
// `/integrations` are exactly the routes F3 asks for.
//
// `/callback` sits outside the group on purpose: it runs before there is a
// session, so it must not be behind the shell's `ProtectedRoute`.

export const metadata = {
  title: config.appName,
  description: 'Run coding agents in sandboxed containers against your repositories.',
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className="h-full">
      <body className="flex min-h-screen flex-col bg-gray-50 text-black antialiased">
        <Providers>{children}</Providers>
      </body>
    </html>
  );
}
