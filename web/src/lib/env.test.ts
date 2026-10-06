import { describe, expect, it } from "vitest";

import { requireEnv } from "./env";

describe("requireEnv", () => {
  it("returns the value when it is set", () => {
    expect(requireEnv("DATABASE_URL", { DATABASE_URL: "postgresql://x" })).toBe("postgresql://x");
  });

  it.each([undefined, ""])("explains how to fix a missing value (%j)", (value) => {
    expect(() => requireEnv("DATABASE_URL", { DATABASE_URL: value })).toThrow(
      "DATABASE_URL is not set. Add it to .env (see .env.example) and try again.",
    );
  });
});
