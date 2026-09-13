/** @type {import('next').NextConfig} */
const nextConfig = {
  // Keep server-side fetches (to the backend) uncached by default; each page
  // in this app is inherently dynamic (chat, live document status).
  experimental: {},
};

module.exports = nextConfig;
