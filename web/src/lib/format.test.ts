import { describe, expect, it } from "vitest";

import { formatDate, formatScore, formatUsd, parseMatchId } from "./format";

describe("format helpers", () => {
  it("formats dollars and says so when there is no value", () => {
    expect(formatUsd("1250000.50")).toBe("$1,250,001");
    expect(formatUsd(null)).toBe("Not stated");
    expect(formatUsd("abc")).toBe("Not stated");
  });

  it("formats dates in UTC", () => {
    expect(formatDate(new Date("2027-01-15T00:00:00Z"))).toBe("Jan 15, 2027");
    expect(formatDate(null)).toBe("Not stated");
  });

  it("shows scores as whole percentages", () => {
    expect(formatScore("0.8555")).toBe("86%");
  });

  it("accepts only numeric match ids", () => {
    expect(parseMatchId("42")).toBe("42");
    expect(parseMatchId("42; DROP TABLE x")).toBeNull();
    expect(parseMatchId(null)).toBeNull();
  });
});
