import type { NextConfig } from "next";

const backendProxyTarget = process.env.BACKEND_PROXY_TARGET ?? "http://127.0.0.1:8000";

const nextConfig: NextConfig = {
  output: "standalone",
  async rewrites() {
    return [
      {
        source: "/api/:path*",
        destination: `${backendProxyTarget}/:path*`,
      },
    ];
  },
};

export default nextConfig;
