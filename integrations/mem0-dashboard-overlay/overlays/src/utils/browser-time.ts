export function formatBrowserLocalTimestamp(value: string | null): string {
  if (value === null) return "--";
  const date = new Date(value);
  return Number.isFinite(date.getTime()) ? date.toLocaleString() : value;
}

export function formatBrowserRelativeTimestamp(
  value: string,
  now = Date.now(),
): string {
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) return value;

  const difference = date.getTime() - now;
  const absoluteDifference = Math.abs(difference);
  const [amount, unit] = absoluteDifference < 60 * 60 * 1000
    ? [Math.round(difference / (60 * 1000)), "minute" as const]
    : absoluteDifference < 24 * 60 * 60 * 1000
      ? [Math.round(difference / (60 * 60 * 1000)), "hour" as const]
      : [Math.round(difference / (24 * 60 * 60 * 1000)), "day" as const];

  return new Intl.RelativeTimeFormat(undefined, { numeric: "auto" }).format(
    amount,
    unit,
  );
}

export function formatBrowserTimelineTick(value: string): string {
  const date = new Date(value);
  return Number.isFinite(date.getTime())
    ? new Intl.DateTimeFormat(undefined, {
        month: "short",
        day: "numeric",
        hour: "2-digit",
        minute: "2-digit",
      }).format(date)
    : value;
}
