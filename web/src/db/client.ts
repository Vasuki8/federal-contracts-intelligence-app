import { Kysely, PostgresDialect } from "kysely";
import { Pool } from "pg";

import { requireEnv } from "@/lib/env";

import type { DB } from "./types";

export function createDb(connectionString: string): Kysely<DB> {
  return new Kysely<DB>({
    dialect: new PostgresDialect({ pool: new Pool({ connectionString, max: 10 }) }),
  });
}

// Reuse one pool per server process (and across hot reloads in dev).
const globalForDb = globalThis as typeof globalThis & { fciDb?: Kysely<DB> };

export function getDb(): Kysely<DB> {
  globalForDb.fciDb ??= createDb(requireEnv("DATABASE_URL"));
  return globalForDb.fciDb;
}
