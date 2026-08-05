/** @type {import('next').NextConfig} */

// The source repo's config proxied a dozen gateway paths through Next's
// rewrites because the browser and the API shared an origin by way of this
// process. Here the control plane is addressed directly by
// NEXT_PUBLIC_AGENT_API_URL, and DR4 puts it on the same host under /api, so
// there is nothing to rewrite. Deployments that split the two must arrange
// CORS with credentials on the control plane rather than proxy through here.
//
// `basePath` mounts the whole app under a prefix, which is how it shares the
// gateway's hostname instead of taking one of its own:
//
//   staging.freeinference.org/agents/*     → this app
//   staging.freeinference.org/agents/api/* → the control plane
//   everything else                        → the gateway
//
// Same origin, so the session cookie needs no CORS and no DNS record is
// involved — and a rollback is a reverse-proxy edit rather than a DNS change
// waiting out a TTL.
//
// Baked in at build time: Next resolves this when the bundle is built, so a
// deployment that changes it rebuilds the image. That is why it is a build
// argument in the compose file and not a runtime variable.
//
// Duplicated from `src/config/env.ts` rather than imported — this file is
// plain CommonJS loaded before the TS pipeline exists. `basePath.test.ts`
// checks the two normalizations agree.
const rawBasePath = (process.env.NEXT_PUBLIC_BASE_PATH || '').trim().replace(/\/+$/, '');
const basePath = !rawBasePath ? '' : rawBasePath.startsWith('/') ? rawBasePath : `/${rawBasePath}`;

const nextConfig = {
  reactStrictMode: true,
  output: 'standalone',
  // Omitted entirely when empty: Next rejects `basePath: ''`, and empty means
  // a deployment that owns its hostname.
  ...(basePath ? { basePath } : {}),
};

module.exports = nextConfig;
