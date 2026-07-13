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
const DAY_IN_MILLISECONDS = 24 * 60 * 60 * 1000;

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
    date_range: {
      from: readIsoDate(searchParams.get("from")),
      to: readIsoDate(searchParams.get("to")),
    },
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
  const id = readNonEmpty(value.id);
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

  const normalizedValue = normalizeFilterValue(value.value);
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
  value: unknown,
): ExplorerFilter["value"] | null {
  if (typeof value === "string") {
    return readNonEmpty(value);
  }
  if (Array.isArray(value)) {
    const items = value
      .map(readNonEmpty)
      .filter((item): item is string => item !== null);
    return items.length === 0 ? null : items;
  }
  if (value !== null && typeof value === "object") {
    const metadata = value as Record<string, unknown>;
    const key = readNonEmpty(metadata.key);
    const metadataValue = readNonEmpty(metadata.value);
    return key === null || metadataValue === null
      ? null
      : { key, value: metadataValue };
  }
  return null;
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

function readIsoDate(raw: string | null): string | null {
  if (
    raw === null
    || !/(?:Z|[+-]\d{2}:\d{2})$/i.test(raw)
    || !Number.isFinite(Date.parse(raw))
  ) {
    return null;
  }
  return raw;
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
