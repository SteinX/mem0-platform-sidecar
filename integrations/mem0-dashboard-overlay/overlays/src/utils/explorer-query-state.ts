import type {
  ExplorerDateRange,
  ExplorerField,
  ExplorerFilter,
  ExplorerMatch,
  ExplorerOperator,
  ExplorerQueryPayload,
} from "@/types/dashboard-explorer";

type DatePreset = "all" | "1d" | "7d" | "30d";
type ExplorerEntityType = "user" | "agent" | "app" | "run";
type ExplorerUrlState = ExplorerQueryPayload & {
  memoryId: string | null;
  requestId: string | null;
  entityType: ExplorerEntityType | null;
  entityId: string | null;
};

export type ExplorerDetailRequestTarget = {
  requestGeneration: number;
  contextGeneration: number;
};

const EXPLORER_FIELDS = new Set<ExplorerField>([
  "entity_type",
  "user_id",
  "agent_id",
  "app_id",
  "run_id",
  "memory_id",
  "category",
  "metadata",
]);
const EXPLORER_OPERATORS = new Set<ExplorerOperator>([
  "equals",
  "not_equals",
  "in",
  "contains",
]);
const EXPLORER_ENTITY_TYPES = new Set<ExplorerEntityType>([
  "user",
  "agent",
  "app",
  "run",
]);
const ENTITY_EXPLORER_FILTER_FIELDS = new Set<ExplorerField>([
  "entity_type",
  "user_id",
  "agent_id",
  "app_id",
  "run_id",
]);
const DAY_IN_MILLISECONDS = 24 * 60 * 60 * 1000;
const ISO_DATE_TIME_PATTERN = new RegExp(
  "^(\\d{4})-(\\d{2})-(\\d{2})T(\\d{2}):(\\d{2}):(\\d{2})"
    + "(?:\\.(\\d+))?(?:Z|([+-])(\\d{2}):(\\d{2}))$",
);

export function createExplorerFilter(
  overrides: Partial<ExplorerFilter> = {},
): ExplorerFilter {
  return {
    id: overrides.id ?? crypto.randomUUID(),
    field: overrides.field ?? "user_id",
    operator: overrides.operator ?? "equals",
    value: overrides.value ?? "",
  };
}

export function normalizeExplorerFilters(filters: unknown): ExplorerFilter[] {
  if (!Array.isArray(filters)) {
    return [];
  }

  const normalized: ExplorerFilter[] = [];
  for (const candidate of filters) {
    const filter = normalizeExplorerFilter(candidate);
    if (filter !== null) {
      normalized.push(filter);
    }
  }
  return normalized;
}

export function normalizeEntityExplorerFilters(
  filters: unknown,
): ExplorerFilter[] {
  return normalizeExplorerFilters(filters).filter((filter) =>
    ENTITY_EXPLORER_FILTER_FIELDS.has(filter.field),
  );
}

export function canApplyExplorerDetailRequest(
  target: ExplorerDetailRequestTarget,
  currentRequestGeneration: number,
  currentContextGeneration: number,
  mounted: boolean,
): boolean {
  return (
    mounted &&
    target.requestGeneration === currentRequestGeneration &&
    target.contextGeneration === currentContextGeneration
  );
}

export function datePresetRange(
  preset: DatePreset,
  now: Date = new Date(),
): ExplorerDateRange {
  if (preset === "all") {
    return { from: null, to: null };
  }

  const days = preset === "1d" ? 1 : preset === "7d" ? 7 : 30;
  return {
    from: new Date(now.getTime() - days * DAY_IN_MILLISECONDS).toISOString(),
    to: now.toISOString(),
  };
}

export function readExplorerUrlState(
  searchParams: URLSearchParams,
): ExplorerUrlState {
  return {
    match: readMatch(searchParams.get("match")),
    filters: readFilters(searchParams.get("filters")),
    date_range: readDateRange(searchParams),
    page: readPage(searchParams.get("page")),
    page_size: 20,
    sort: "created_at_desc",
    memoryId: readNonEmpty(searchParams.get("memoryId")),
    requestId: readNonEmpty(searchParams.get("requestId")),
    entityType: readEntityType(searchParams.get("entityType")),
    entityId: readNonEmpty(searchParams.get("entityId")),
  };
}

export function writeExplorerUrlState(
  current: URLSearchParams,
  query: ExplorerQueryPayload,
): URLSearchParams {
  const next = new URLSearchParams(current.toString());
  next.set("match", query.match);
  next.set("filters", JSON.stringify(normalizeExplorerFilters(query.filters)));
  writeOptional(next, "from", query.date_range.from);
  writeOptional(next, "to", query.date_range.to);
  next.set("page", String(query.page));
  return next;
}

function normalizeExplorerFilter(candidate: unknown): ExplorerFilter | null {
  if (candidate === null || typeof candidate !== "object") {
    return null;
  }

  const value = candidate as Record<string, unknown>;
  const id = typeof value.id === "string" && value.id.trim() !== ""
    ? value.id
    : null;
  const field = value.field;
  const operator = value.operator;
  if (
    id === null
    || typeof field !== "string"
    || !EXPLORER_FIELDS.has(field as ExplorerField)
    || typeof operator !== "string"
    || !EXPLORER_OPERATORS.has(operator as ExplorerOperator)
  ) {
    return null;
  }

  const normalizedValue = normalizeFilterValue(
    field as ExplorerField,
    operator as ExplorerOperator,
    value.value,
  );
  if (normalizedValue === null) {
    return null;
  }
  return {
    id,
    field: field as ExplorerField,
    operator: operator as ExplorerOperator,
    value: normalizedValue,
  };
}

function normalizeFilterValue(
  field: ExplorerField,
  operator: ExplorerOperator,
  value: unknown,
): ExplorerFilter["value"] | null {
  if (field === "metadata") {
    return operator === "contains" ? normalizeMetadataValue(value) : null;
  }
  if (operator === "equals" || operator === "not_equals") {
    return normalizeScalarValue(field, value);
  }
  if (operator === "in") {
    return normalizeInValue(field, value);
  }
  return null;
}

function normalizeMetadataValue(
  value: unknown,
): { key: string; value: string } | null {
  if (
    value === null
    || Array.isArray(value)
    || typeof value !== "object"
  ) {
    return null;
  }
  const metadata = value as Record<string, unknown>;
  const keys = Object.keys(metadata);
  if (keys.length !== 2 || !keys.includes("key") || !keys.includes("value")) {
    return null;
  }
  const key = readNonEmpty(metadata.key);
  const metadataValue = readNonEmpty(metadata.value);
  return key === null || metadataValue === null
    ? null
    : { key, value: metadataValue };
}

function normalizeScalarValue(
  field: ExplorerField,
  value: unknown,
): string | null {
  const normalized = readNonEmpty(value);
  if (
    normalized === null
    || (field === "entity_type"
      && !EXPLORER_ENTITY_TYPES.has(normalized as ExplorerEntityType))
  ) {
    return null;
  }
  return normalized;
}

function normalizeInValue(
  field: ExplorerField,
  value: unknown,
): string[] | null {
  if (!Array.isArray(value) || value.length === 0) {
    return null;
  }
  const normalized: string[] = [];
  for (const item of value) {
    const scalar = normalizeScalarValue(field, item);
    if (scalar === null) {
      return null;
    }
    normalized.push(scalar);
  }
  return normalized;
}

function readFilters(raw: string | null): ExplorerFilter[] {
  if (raw === null) {
    return [];
  }
  try {
    return normalizeExplorerFilters(JSON.parse(raw));
  } catch {
    return [];
  }
}

function readMatch(raw: string | null): ExplorerMatch {
  return raw === "any" ? "any" : "all";
}

function readPage(raw: string | null): number {
  if (raw === null || !/^\d+$/.test(raw)) {
    return 1;
  }
  const page = Number(raw);
  return Number.isSafeInteger(page) && page >= 1 ? page : 1;
}

function readDateRange(searchParams: URLSearchParams): ExplorerDateRange {
  const from = parseIsoDate(searchParams.get("from"));
  const to = parseIsoDate(searchParams.get("to"));
  if (
    from !== null
    && to !== null
    && from.epochMicroseconds > to.epochMicroseconds
  ) {
    return { from: null, to: null };
  }
  return {
    from: from?.value ?? null,
    to: to?.value ?? null,
  };
}

function parseIsoDate(
  raw: string | null,
): { value: string; epochMicroseconds: bigint } | null {
  if (raw === null) {
    return null;
  }
  const match = ISO_DATE_TIME_PATTERN.exec(raw);
  if (match === null) {
    return null;
  }

  const year = Number(match[1]);
  const month = Number(match[2]);
  const day = Number(match[3]);
  const hour = Number(match[4]);
  const minute = Number(match[5]);
  const second = Number(match[6]);
  const offsetHour = match[9] === undefined ? 0 : Number(match[9]);
  const offsetMinute = match[10] === undefined ? 0 : Number(match[10]);
  if (
    year < 1
    || month < 1
    || month > 12
    || day < 1
    || day > daysInMonth(year, month)
    || hour > 23
    || minute > 59
    || second > 59
    || offsetHour > 23
    || offsetMinute > 59
  ) {
    return null;
  }

  const utcCalendar = new Date(0);
  utcCalendar.setUTCFullYear(year, month - 1, day);
  utcCalendar.setUTCHours(hour, minute, second, 0);
  const calendarMilliseconds = utcCalendar.getTime();
  if (!Number.isFinite(calendarMilliseconds)) {
    return null;
  }

  const fraction = (match[7] ?? "").slice(0, 6).padEnd(6, "0");
  const offsetSign = BigInt(match[8] === "-" ? -1 : 1);
  const offsetMicroseconds = offsetSign
    * BigInt(offsetHour * 60 + offsetMinute)
    * BigInt(60)
    * BigInt(1_000_000);
  const epochMicroseconds = BigInt(calendarMilliseconds) * BigInt(1_000)
    + BigInt(fraction)
    - offsetMicroseconds;
  return { value: raw, epochMicroseconds };
}

function daysInMonth(year: number, month: number): number {
  if (month === 2) {
    const leapYear = year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0);
    return leapYear ? 29 : 28;
  }
  return [4, 6, 9, 11].includes(month) ? 30 : 31;
}

function readEntityType(raw: string | null): ExplorerEntityType | null {
  return raw !== null && EXPLORER_ENTITY_TYPES.has(raw as ExplorerEntityType)
    ? raw as ExplorerEntityType
    : null;
}

function readNonEmpty(raw: unknown): string | null {
  if (typeof raw !== "string") {
    return null;
  }
  const value = raw.trim();
  return value === "" ? null : value;
}

function writeOptional(
  searchParams: URLSearchParams,
  key: string,
  value: string | null,
): void {
  if (value === null) {
    searchParams.delete(key);
    return;
  }
  searchParams.set(key, value);
}
