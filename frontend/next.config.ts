import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Standalone output ships a minimal server.js + only the node_modules
  // files actually needed at runtime -- required for a lean production
  // Docker image (see frontend/Dockerfile).
  output: "standalone",
};

export default nextConfig;
