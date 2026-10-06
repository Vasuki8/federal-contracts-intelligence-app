import type { Metadata } from "next";

import { getDb } from "@/db/client";
import { getReviewQueue, groupByNotice, type NoticeGroup, type QueueItem } from "@/db/matches";
import { formatDate, formatScore, formatUsd } from "@/lib/format";

import { confirmMatch, rejectMatch } from "./actions";

export const dynamic = "force-dynamic";
export const metadata: Metadata = { title: "Review possible incumbents", robots: "noindex" };

export default async function MatchesPage() {
  const groups = groupByNotice(await getReviewQueue(getDb()));
  return (
    <main className="mx-auto max-w-5xl px-6 py-10">
      <h1 className="text-2xl font-semibold tracking-tight">Review possible incumbents</h1>
      <p className="mt-2 text-gray-600">
        These notices have no confirmed incumbent yet. Confirm the contract the notice replaces,
        or reject the ones that are wrong. Every decision is kept and used to check the matcher.
      </p>
      {groups.length === 0 ? (
        <p className="mt-10 text-gray-500">Nothing to review right now.</p>
      ) : (
        groups.map((group) => <NoticeCard key={group.notice.noticeId} group={group} />)
      )}
    </main>
  );
}

function NoticeCard({ group: { notice, items } }: { group: NoticeGroup }) {
  return (
    <section className="mt-8 rounded-lg border border-gray-200 p-5">
      <h2 className="text-lg font-medium">
        {notice.samLink ? (
          <a className="underline" href={notice.samLink} target="_blank" rel="noreferrer">
            {notice.noticeTitle ?? notice.noticeId}
          </a>
        ) : (
          (notice.noticeTitle ?? notice.noticeId)
        )}
      </h2>
      <p className="mt-1 text-sm text-gray-600">
        {notice.agency ?? "Agency not stated"} · Responses due {formatDate(notice.deadline)}
      </p>
      <ul className="mt-4 space-y-4">
        {items.map((item) => (
          <Candidate key={item.matchId} item={item} />
        ))}
      </ul>
    </section>
  );
}

function Candidate({ item }: { item: QueueItem }) {
  return (
    <li className="rounded-md bg-gray-50 p-4">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <p className="font-medium">
          {item.awardLink ? (
            <a className="underline" href={item.awardLink} target="_blank" rel="noreferrer">
              {item.piid ?? item.awardKey}
            </a>
          ) : (
            (item.piid ?? item.awardKey)
          )}{" "}
          · {item.company ?? "Company not stated"}
        </p>
        <p className="text-sm text-gray-600">Match score {formatScore(item.score)}</p>
      </div>
      <p className="mt-1 text-sm text-gray-600">
        Value {formatUsd(item.value)} · Ends {formatDate(item.ends)}
      </p>
      {item.reasons.length > 0 && (
        <ul className="mt-2 list-disc pl-5 text-sm">
          {item.reasons.map((reason) => (
            <li key={reason}>{reason}</li>
          ))}
        </ul>
      )}
      <div className="mt-3 flex gap-3">
        <form action={confirmMatch}>
          <input type="hidden" name="matchId" value={item.matchId} />
          <button className="rounded bg-gray-900 px-3 py-1.5 text-sm text-white" type="submit">
            This is the incumbent
          </button>
        </form>
        <form action={rejectMatch}>
          <input type="hidden" name="matchId" value={item.matchId} />
          <button className="rounded border border-gray-300 px-3 py-1.5 text-sm" type="submit">
            Not the incumbent
          </button>
        </form>
      </div>
    </li>
  );
}
