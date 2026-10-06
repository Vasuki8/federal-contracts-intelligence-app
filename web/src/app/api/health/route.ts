import { NextResponse } from "next/server";

import { getDb } from "@/db/client";
import { checkDb } from "@/db/health";

export const dynamic = "force-dynamic";

export async function GET(): Promise<NextResponse> {
  const health = await checkDb(getDb);
  const ok = health.db === "up";
  return NextResponse.json({ ok, ...health }, { status: ok ? 200 : 503 });
}
