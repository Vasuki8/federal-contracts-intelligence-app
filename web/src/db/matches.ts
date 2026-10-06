import { type Kysely, sql } from "kysely";

import type { DB, Json } from "./types";

export type ReviewDecision = "confirmed" | "rejected";

export type QueueItem = {
  matchId: string;
  noticeId: string;
  noticeTitle: string | null;
  agency: string | null;
  deadline: Date | null;
  samLink: string | null;
  awardKey: string;
  piid: string | null;
  company: string | null;
  value: string | null;
  ends: Date | null;
  score: string;
  reasons: string[];
  awardLink: string | null;
};

/** Low-confidence matches waiting for a person: possible incumbents of active notices. */
export function reviewQueueQuery(db: Kysely<DB>, limit: number) {
  return db
    .selectFrom("notice_award_matches as m")
    .innerJoin("notices as n", "n.notice_id", "m.notice_id")
    .innerJoin("awards as a", "a.award_key", "m.award_key")
    .where("m.kind", "=", "incumbent")
    .where("m.status", "=", "auto")
    .where("m.shown", "=", "possible")
    .where("n.active", "=", true)
    .select([
      "m.id as matchId",
      "m.notice_id as noticeId",
      "n.title as noticeTitle",
      "n.full_parent_path_name as agency",
      "n.response_deadline as deadline",
      "n.ui_link as samLink",
      "m.award_key as awardKey",
      "a.piid as piid",
      "a.recipient_name as company",
      sql<string | null>`coalesce(a.total_value, a.current_total_value)::text`.as("value"),
      sql<Date | null>`coalesce(a.ultimate_end, a.ordering_period_end)`.as("ends"),
      sql<string>`m.score::text`.as("score"),
      "m.evidence as evidence",
      "a.usaspending_url as awardLink",
    ])
    .orderBy("n.response_deadline", (order) => order.asc().nullsLast())
    .orderBy("m.notice_id")
    .orderBy("m.rank")
    .limit(limit);
}

export async function getReviewQueue(db: Kysely<DB>, limit = 150): Promise<QueueItem[]> {
  const rows = await reviewQueueQuery(db, limit).execute();
  return rows.map(({ evidence, ...row }) => ({ ...row, reasons: reasonsFrom(evidence) }));
}

/** The plain-English reasons the matcher stored with the match. */
export function reasonsFrom(evidence: Json): string[] {
  if (evidence === null || typeof evidence !== "object" || Array.isArray(evidence)) return [];
  const reasons = evidence.reasons;
  return Array.isArray(reasons) ? reasons.filter((r): r is string => typeof r === "string") : [];
}

export type NoticeGroup = { notice: QueueItem; items: QueueItem[] };

/** Group queue items by notice, keeping the queue's order. */
export function groupByNotice(items: QueueItem[]): NoticeGroup[] {
  const groups = new Map<string, NoticeGroup>();
  for (const item of items) {
    const group = groups.get(item.noticeId);
    if (group) group.items.push(item);
    else groups.set(item.noticeId, { notice: item, items: [item] });
  }
  return [...groups.values()];
}

/**
 * Record a decision (never overwritten: one `match_reviews` row each) and apply it:
 * a confirmed match becomes the shown incumbent and its notice's other automatic matches
 * are hidden; a rejected one is hidden. The matcher never changes either again.
 */
export async function recordReview(
  db: Kysely<DB>,
  input: { matchId: string; decision: ReviewDecision; reviewer: string; note?: string },
): Promise<boolean> {
  return db.transaction().execute(async (trx) => {
    const match = await trx
      .selectFrom("notice_award_matches")
      .select(["id", "notice_id", "award_key", "kind"])
      .where("id", "=", input.matchId)
      .forUpdate()
      .executeTakeFirst();
    if (!match || match.kind !== "incumbent") return false;
    await trx
      .insertInto("match_reviews")
      .values({
        match_id: match.id,
        notice_id: match.notice_id,
        award_key: match.award_key,
        decision: input.decision,
        reviewer: input.reviewer,
        note: input.note ?? null,
      })
      .execute();
    await trx
      .updateTable("notice_award_matches")
      .set({
        status: input.decision,
        shown: input.decision === "confirmed" ? "incumbent" : "hidden",
        updated_at: sql`now()`,
      })
      .where("id", "=", match.id)
      .execute();
    if (input.decision === "confirmed") {
      await trx
        .updateTable("notice_award_matches")
        .set({ shown: "hidden", updated_at: sql`now()` })
        .where("notice_id", "=", match.notice_id)
        .where("kind", "=", "incumbent")
        .where("status", "=", "auto")
        .where("id", "!=", match.id)
        .execute();
    }
    return true;
  });
}
