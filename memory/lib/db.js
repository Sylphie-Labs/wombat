// Shared Postgres pool for the wombat agent-memory pipeline.
// Defaults target the local docker DB (memory/docker-compose.yml); override via env.
const { Pool } = require("pg");

const pool = new Pool({
  host: process.env.WOMBAT_MEMORY_PG_HOST || "localhost",
  port: parseInt(process.env.WOMBAT_MEMORY_PG_PORT || "5544", 10),
  user: process.env.WOMBAT_MEMORY_PG_USER || "wombat",
  password: process.env.WOMBAT_MEMORY_PG_PASSWORD || "wombat-memory-local",
  database: process.env.WOMBAT_MEMORY_PG_DATABASE || "wombat_memory",
  max: 4,
  connectionTimeoutMillis: 4000,
  idleTimeoutMillis: 2000,
});

module.exports = { pool };
