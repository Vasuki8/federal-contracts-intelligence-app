import { describe, expect, it } from "vitest";

import { fakeDb, recordingDriver } from "./fake-db";
import { groupByNotice, type QueueItem, reasonsFrom, recordReview, reviewQueueQuery } from "./matches";

describe("reviewQueueQuery", () => {
  it("lists automatic possible incumbents of active notices, soonest deadline first", () => {
    const { sql, parameters } = reviewQueueQuery(fakeDb(), 50).compile();
    expect(sql).toContain('from "notice_award_matches" as "m"');
    expect(sql).toContain('order by "n"."response_deadline" asc nulls last');
    expect(parameters).toEqual(["incumbent", "auto", "possible", true, 50]);
  });
});

describe("reasonsFrom", () => {
  it("keeps only the reason strings", () => {
    expect(reasonsFrom({ reasons: ["Same office", 3, "Ends soon"] })).toEqual([
      "Same office",
      "Ends soon",
    ]);
    expect(reasonsFrom(null)).toEqual([]);
    expect(reasonsFrom([1, 2])).toEqual([]);
  });
});

describe("groupByNotice", () => {
  it("groups in queue order", () => {
    const item = (matchId: string, noticeId: string) => ({ matchId, noticeId }) as QueueItem;
    const groups = groupByNotice([item("1", "A"), item("2", "B"), item("3", "A")]);
    expect(groups.map((g) => [g.notice.noticeId, g.items.map((i) => i.matchId)])).toEqual([
      ["A", ["1", "3"]],
      ["B", ["2"]],
    ]);
  });
});

describe("recordReview", () => {
  const match = { id: "7", notice_id: "N1", award_key: "A1", kind: "incumbent" };

  it("records a confirmation, shows it, and hides the notice's other automatic matches", async () => {
    const { driver, queries } = recordingDriver((sql) => (sql.startsWith("select") ? [match] : []));
    await expect(
      recordReview(fakeDb(driver), { matchId: "7", decision: "confirmed", reviewer: "admin" }),
    ).resolves.toBe(true);
    const sql = queries.map((q) => q.sql);
    expect(sql[0]).toMatch(/for update$/);
    expect(sql[1]).toContain('insert into "match_reviews"');
    expect(queries[1]?.parameters).toEqual(["7", "N1", "A1", "confirmed", "admin", null]);
    expect(queries[2]?.parameters).toEqual(["confirmed", "incumbent", "7"]);
    expect(sql[3]).toContain('"id" != $');
    expect(sql).toHaveLength(4);
  });

  it("records a rejection and hides only that match", async () => {
    const { driver, queries } = recordingDriver((sql) => (sql.startsWith("select") ? [match] : []));
    await recordReview(fakeDb(driver), { matchId: "7", decision: "rejected", reviewer: "admin" });
    expect(queries[2]?.parameters).toEqual(["rejected", "hidden", "7"]);
    expect(queries).toHaveLength(3);
  });

  it("does nothing for an unknown match", async () => {
    const { driver, queries } = recordingDriver(() => []);
    await expect(
      recordReview(fakeDb(driver), { matchId: "9", decision: "rejected", reviewer: "admin" }),
    ).resolves.toBe(false);
    expect(queries).toHaveLength(1);
  });
});
