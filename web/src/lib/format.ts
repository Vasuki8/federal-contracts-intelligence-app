/** Display helpers: plain, short, and never inventing a value that isn't there. */

const usd = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
  maximumFractionDigits: 0,
});

export function formatUsd(value: string | null): string {
  if (value === null || value.trim() === "") return "Not stated";
  const amount = Number(value);
  return Number.isFinite(amount) ? usd.format(amount) : "Not stated";
}

export function formatDate(value: Date | null): string {
  if (value === null) return "Not stated";
  return value.toLocaleDateString("en-US", {
    year: "numeric",
    month: "short",
    day: "numeric",
    timeZone: "UTC",
  });
}

export function formatScore(score: string): string {
  const value = Number(score);
  return Number.isFinite(value) ? `${Math.round(value * 100)}%` : "–";
}

/** A match id from a form field: digits only, so it can't carry anything else. */
export function parseMatchId(value: FormDataEntryValue | null): string | null {
  return typeof value === "string" && /^\d{1,18}$/.test(value) ? value : null;
}
