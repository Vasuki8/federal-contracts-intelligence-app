import { afterEach, describe, expect, it, vi } from "vitest";

import { getDb } from "./client";
import { fakeDb } from "./fake-db";

describe("generated database types", () => {
  it("let Kysely build typed queries against the migrated schema", () => {
    const query = fakeDb().selectFrom("alembic_version").select("version_num").compile();
    expect(query.sql).toBe('select "version_num" from "alembic_version"');
  });
});

describe("getDb", () => {
  afterEach(() => {
    vi.unstubAllEnvs();
  });

  it("fails with a clear message when DATABASE_URL is missing", () => {
    vi.stubEnv("DATABASE_URL", "");
    expect(() => getDb()).toThrow("DATABASE_URL is not set");
  });
});
