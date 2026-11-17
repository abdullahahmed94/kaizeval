// Display helpers. Nothing here computes a statistic; values come from the API and are only formatted.

/** The API stores UTC timestamps; some are serialized without a zone suffix. */
export function parseDate(value: string | null | undefined): Date | null {
  if (!value) return null;
  const hasZone = /(Z|[+-]\d{2}:?\d{2})$/i.test(value);
  const date = new Date(hasZone ? value : `${value}Z`);
  return Number.isNaN(date.getTime()) ? null : date;
}

export function formatDate(value: string | null | undefined): string {
  const date = parseDate(value);
  if (!date) return "—";
  return date.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

/** A 0..1 score as a percentage with one decimal. */
export function formatPercent(value: number | null | undefined): string {
  if (value === null || value === undefined) return "n/a";
  return `${(value * 100).toFixed(1)}%`;
}

/** A difference already expressed in percentage points, for example "+12.3 pp". */
export function formatPp(value: number | null | undefined): string {
  if (value === null || value === undefined) return "n/a";
  const sign = value < 0 ? "−" : "+";
  return `${sign}${Math.abs(value).toFixed(1)} pp`;
}

/** A relative change already expressed in percent, for example "+27.0%". */
export function formatRelative(value: number | null | undefined): string {
  if (value === null || value === undefined) return "n/a";
  const sign = value < 0 ? "−" : "+";
  return `${sign}${Math.abs(value).toFixed(1)}%`;
}

export function formatInt(value: number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  return value.toLocaleString();
}

export function formatMs(value: number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  return `${value.toLocaleString()} ms`;
}

export function formatCost(value: number | null | undefined): string {
  if (value === null || value === undefined) return "not available";
  return value.toFixed(4);
}

export function formatBytes(value: number): string {
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / 1024 / 1024).toFixed(1)} MB`;
}

export function shortId(value: string | null | undefined, length = 8): string {
  if (!value) return "—";
  return value.length > length ? value.slice(0, length) : value;
}

/** "multi_turn_consistency" -> "Multi turn consistency". */
export function humanize(value: string | null | undefined): string {
  if (!value) return "—";
  const text = value.replace(/[_-]+/g, " ").trim();
  return text.charAt(0).toUpperCase() + text.slice(1);
}

export function formatValue(value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "string") return value;
  return JSON.stringify(value);
}

export function parseOptionalNumber(text: string): number | null {
  const trimmed = text.trim();
  if (!trimmed) return null;
  const value = Number(trimmed);
  return Number.isFinite(value) ? value : null;
}
