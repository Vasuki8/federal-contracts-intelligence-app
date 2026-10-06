"use server";

import { revalidatePath } from "next/cache";
import { headers } from "next/headers";

import { getDb } from "@/db/client";
import { recordReview, type ReviewDecision } from "@/db/matches";
import { ADMIN_USER, checkAdmin } from "@/lib/admin-auth";
import { parseMatchId } from "@/lib/format";

async function review(formData: FormData, decision: ReviewDecision): Promise<void> {
  // The proxy already asks for the password; check again so an action is never open.
  const access = checkAdmin((await headers()).get("authorization"), process.env.ADMIN_PASSWORD);
  if (access !== "allowed") throw new Error("Sign in to use the admin pages.");
  const matchId = parseMatchId(formData.get("matchId"));
  if (matchId === null) throw new Error("That match could not be found.");
  await recordReview(getDb(), { matchId, decision, reviewer: ADMIN_USER });
  revalidatePath("/admin/matches");
}

export async function confirmMatch(formData: FormData): Promise<void> {
  await review(formData, "confirmed");
}

export async function rejectMatch(formData: FormData): Promise<void> {
  await review(formData, "rejected");
}
