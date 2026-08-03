/** @type {import('next').NextConfig} */

// The source repo's config proxied a dozen gateway paths through Next's
// rewrites because the browser and the API shared an origin by way of this
// process. Here the control plane is addressed directly by
// NEXT_PUBLIC_AGENT_API_URL, and DR4 puts it on the same host under /api, so
// there is nothing to rewrite. Deployments that split the two must arrange
// CORS with credentials on the control plane rather than proxy through here.
const nextConfig = {
  reactStrictMode: true,
  output: 'standalone',
};

module.exports = nextConfig;
