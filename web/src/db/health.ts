import type { Kysely } from "kysely";

import type { DB } from "./types";

export type DbHealth = { db: "up"; schemaVersion: string | null } | { db: "down" };

/** Report whether the database answers, and which Alembic migration it is on. */
export async function checkDb(getDb: () => Kysely<DB>): Promise<DbHealth> {
  try {
    const row = await getDb()
      .selectFrom("alembic_version")
      .select("version_num")
      .executeTakeFirst();
    return { db: "up", schemaVersion: row?.version_num ?? null };
  } catch {
    return { db: "down" };
  }
}
