import { existsSync } from "node:fs";
import path from "node:path";
import type { NextConfig } from "next";

// One .env at the repo root is shared by the pipeline and the web app.
// Variables already set in the environment (e.g. in CI) take precedence.
const rootEnvFile = path.resolve(process.cwd(), "..", ".env");
if (existsSync(rootEnvFile)) {
  process.loadEnvFile(rootEnvFile);
}

const nextConfig: NextConfig = {};

export default nextConfig;
