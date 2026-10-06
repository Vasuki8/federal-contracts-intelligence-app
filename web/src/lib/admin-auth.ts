/**
 * HTTP Basic auth for /admin until real accounts arrive (M3). The password comes from
 * ADMIN_PASSWORD; when it isn't set, the admin pages don't exist.
 */

export const ADMIN_USER = "admin";
export const ADMIN_REALM = 'Basic realm="Admin", charset="UTF-8"';

export type Credentials = { user: string; password: string };

/** Read "Basic base64(user:password)"; anything else is null. */
export function parseBasicAuth(header: string | null | undefined): Credentials | null {
  if (!header) return null;
  const encoded = /^Basic\s+([A-Za-z0-9+/=]+)$/i.exec(header.trim())?.[1];
  if (!encoded) return null;
  let decoded: string;
  try {
    decoded = atob(encoded);
  } catch {
    return null;
  }
  const colon = decoded.indexOf(":");
  if (colon < 0) return null;
  return { user: decoded.slice(0, colon), password: decoded.slice(colon + 1) };
}

/** Compare without stopping at the first difference, so timing reveals less. */
export function safeEqual(a: string, b: string): boolean {
  let difference = a.length ^ b.length;
  for (let i = 0; i < Math.max(a.length, b.length); i++) {
    difference |= (a.charCodeAt(i) || 0) ^ (b.charCodeAt(i) || 0);
  }
  return difference === 0;
}

export type AdminAccess = "allowed" | "denied" | "disabled";

export function checkAdmin(header: string | null | undefined, password: string | undefined): AdminAccess {
  if (!password) return "disabled";
  const credentials = parseBasicAuth(header);
  if (!credentials) return "denied";
  const userOk = safeEqual(credentials.user, ADMIN_USER);
  const passwordOk = safeEqual(credentials.password, password);
  return userOk && passwordOk ? "allowed" : "denied";
}
