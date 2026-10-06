import { describe, expect, it } from "vitest";

import { driverFailingWith, driverReturning, fakeDb } from "./fake-db";
import { checkDb } from "./health";

describe("checkDb", () => {
  it("reports the migration version when the database answers", async () => {
    const db = fakeDb(driverReturning([{ version_num: "0001" }]));
    await expect(checkDb(() => db)).resolves.toEqual({ db: "up", schemaVersion: "0001" });
  });

  it("reports a null version before any migration has run", async () => {
    const db = fakeDb(driverReturning([]));
    await expect(checkDb(() => db)).resolves.toEqual({ db: "up", schemaVersion: null });
  });

  it("reports down when the database cannot be reached", async () => {
    const db = fakeDb(driverFailingWith("connection refused"));
    await expect(checkDb(() => db)).resolves.toEqual({ db: "down" });
  });

  it("reports down when the database is not configured", async () => {
    const missingConfig = () => {
      throw new Error("DATABASE_URL is not set");
    };
    await expect(checkDb(missingConfig)).resolves.toEqual({ db: "down" });
  });
});
