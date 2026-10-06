import { NextResponse, type NextRequest } from "next/server";

import { ADMIN_REALM, checkAdmin } from "@/lib/admin-auth";

/** Ask for the admin password on every /admin page and server action. */
export function proxy(request: NextRequest): NextResponse {
  const access = checkAdmin(request.headers.get("authorization"), process.env.ADMIN_PASSWORD);
  if (access === "allowed") return NextResponse.next();
  if (access === "disabled") return new NextResponse("Not found", { status: 404 });
  return new NextResponse("Sign in to use the admin pages.", {
    status: 401,
    headers: { "WWW-Authenticate": ADMIN_REALM },
  });
}

export const config = {
  matcher: ["/admin", "/admin/:path*"],
};
