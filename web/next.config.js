/** @type {import('next').NextConfig} */

// The source repo's config proxied a dozen gateway paths through Next's
// rewrites because the browser and the API shared an origin by way of this
// process. Here the control plane is addressed directly by
// NEXT_PUBLIC_AGENT_API_URL, and DR4 puts it on the same host under /api, so
// there is nothing to rewrite. Deployments that split the two must arrange
// CORS with credentials on the control plane rather than proxy through here.
//
// `basePath` mounts the whole app under a prefix. **The deployed shape does
// not use it**: this stack gets its own origin, on the gateway's hostname but
// its own port, and serves from the root of it —
//
//   staging.freeinference.org:8443/*     → this app
//   staging.freeinference.org:8443/api/* → the control plane
//
// — because an origin is scheme + host + port, and a path prefix is not a
// boundary. Mounting under the gateway's `/agents` was the earlier shape and
// it let this app read the gateway's `sessionStorage` access token, which
// matters when the app's normal input is model output and terminal contents.
// See `deploy/reverse-proxy/agents-own-origin.conf`.
//
// The prefix support stays for a deployment that wants it. If you use it, the
// caveat below about hand-written URLs is the thing that bites.
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
