// Display formatting helpers. Kept dependency-free so the SPA stays small.

const usdFmt = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});

const signedUsdFmt = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
  signDisplay: "always",
});

const intFmt = new Intl.NumberFormat("en-US");

export function formatUsd(value: number): string {
  return usdFmt.format(value);
}

export function formatSignedUsd(value: number): string {
  return signedUsdFmt.format(value);
}

export function formatPercent(value: number, fractionDigits = 2): string {
  return `${value.toFixed(fractionDigits)}%`;
}

export function formatCount(value: number): string {
  return intFmt.format(value);
}

export function formatRelativeTime(iso: string, nowMs: number = Date.now()): string {
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return iso;
  const diffSec = Math.round((nowMs - t) / 1000);
  if (diffSec < 5) return "just now";
  if (diffSec < 60) return `${diffSec}s ago`;
  const diffMin = Math.round(diffSec / 60);
  if (diffMin < 60) return `${diffMin}m ago`;
  const diffH = Math.round(diffMin / 60);
  if (diffH < 24) return `${diffH}h ago`;
  const diffD = Math.round(diffH / 24);
  return `${diffD}d ago`;
}
