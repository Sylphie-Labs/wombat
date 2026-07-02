// Shared Postgres pool for the wombat agent-memory pipeline.
// Non-secret defaults target the local docker DB (memory/docker-compose.yml); override via env.
// Secrets (the DB password) live ONLY in env — load the repo-root .env via Node's built-in
// loader (>=20.12; no dependency). See .env.example for the required variables.
const path = require("node:path");
try {
  process.loadEnvFile(path.resolve(__dirname, "..", "..", ".env"));
} catch {
  // .env is optional here — the variable may already be set in the process environment.
}

const { Pool } = require("pg");

const pool = new Pool({
  host: process.env.WOMBAT_MEMORY_PG_HOST || "localhost",
  port: parseInt(process.env.WOMBAT_MEMORY_PG_PORT || "5544", 10),
  user: process.env.WOMBAT_MEMORY_PG_USER || "wombat",
  password: process.env.WOMBAT_MEMORY_PG_PASSWORD,
  database: process.env.WOMBAT_MEMORY_PG_DATABASE || "wombat_memory",
  max: 4,
  connectionTimeoutMillis: 4000,
  idleTimeoutMillis: 2000,
});

module.exports = { pool };
