/** @type {import('next').NextConfig} */
const nextConfig = {
  // Static export: FastAPI serves dashboard/web/out at / (single server).
  output: 'export',
  // In `npm run dev`, proxy API calls to the FastAPI backend on :8501.
  async rewrites() {
    return [
      { source: '/api/:path*', destination: 'http://localhost:8501/api/:path*' },
      { source: '/jd/:path*', destination: 'http://localhost:8501/jd/:path*' },
      { source: '/answers/:path*', destination: 'http://localhost:8501/answers/:path*' },
      { source: '/files/:path*', destination: 'http://localhost:8501/files/:path*' },
    ];
  },
};

export default nextConfig;
