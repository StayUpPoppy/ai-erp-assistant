/** @type {import('next').NextConfig} */
// 浏览器请求 `/api/orchestrator/*` 由 Next 转发到本机 FastAPI，避免跨域与「页面是局域网 IP、API 写死 127.0.0.1」导致的 Failed to fetch。
const orchestratorProxyTarget =
  process.env.ORCHESTRATOR_PROXY_TARGET?.trim() || "http://127.0.0.1:8020";

const nextConfig = {
  reactStrictMode: true,
  async rewrites() {
    const dest = orchestratorProxyTarget.replace(/\/$/, "");
    return [{ source: "/api/orchestrator/:path*", destination: `${dest}/:path*` }];
  },
};

export default nextConfig;
