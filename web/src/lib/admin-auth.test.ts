import { describe, expect, it } from "vitest";

import { checkAdmin, parseBasicAuth, safeEqual } from "./admin-auth";

const basic = (user: string, password: string) => `Basic ${btoa(`${user}:${password}`)}`;

describe("parseBasicAuth", () => {
  it("reads the user and a password that contains colons", () => {
    expect(parseBasicAuth(basic("admin", "a:b:c"))).toEqual({ user: "admin", password: "a:b:c" });
  });

  it("rejects missing, malformed and non-Basic headers", () => {
    expect(parseBasicAuth(null)).toBeNull();
    expect(parseBasicAuth("Bearer abc")).toBeNull();
    expect(parseBasicAuth("Basic !!!")).toBeNull();
    expect(parseBasicAuth(`Basic ${btoa("no-colon")}`)).toBeNull();
  });
});

describe("checkAdmin", () => {
  it("is disabled when no password is configured", () => {
    expect(checkAdmin(basic("admin", "x"), undefined)).toBe("disabled");
    expect(checkAdmin(basic("admin", "x"), "")).toBe("disabled");
  });

  it("allows only the admin user with the right password", () => {
    expect(checkAdmin(basic("admin", "s3cret"), "s3cret")).toBe("allowed");
    expect(checkAdmin(basic("admin", "wrong"), "s3cret")).toBe("denied");
    expect(checkAdmin(basic("someone", "s3cret"), "s3cret")).toBe("denied");
    expect(checkAdmin(null, "s3cret")).toBe("denied");
  });
});

describe("safeEqual", () => {
  it("compares whole strings", () => {
    expect(safeEqual("abc", "abc")).toBe(true);
    expect(safeEqual("abc", "abd")).toBe(false);
    expect(safeEqual("abc", "abcd")).toBe(false);
    expect(safeEqual("", "")).toBe(true);
  });
});
