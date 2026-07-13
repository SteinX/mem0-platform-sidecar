import type {
  ExplorerDateRange,
  ExplorerFilter,
  ExplorerMatch,
} from "@/types/dashboard-explorer";
import type { SidecarTraceQuery } from "@/types/sidecar";

export type RequestTraceQueryState = {
  match: ExplorerMatch;
  filters: ExplorerFilter[];
  date_range: ExplorerDateRange;
  operation: SidecarTraceQuery["operation"];
  has_results: boolean | null;
  page: number;
  page_size: number;
};

export type TraceDetailRequest = {
  generation: number;
  targetId: string;
};

type BaseTraceQueryState = Pick<
  RequestTraceQueryState,
  "match" | "filters" | "date_range" | "page" | "page_size"
>;

const EVENT_SCAN_HORIZON = 5_000;
const DEFAULT_PAGE_SIZE = 20;
const ENTITY_FIELDS = new Set<ExplorerFilter["field"]>([
  "user_id",
  "agent_id",
  "app_id",
  "run_id",
]);

export function normalizeRequestTraceQueryState(
  base: BaseTraceQueryState,
  searchParams: URLSearchParams,
): RequestTraceQueryState {
  return {
    match: "all",
    filters: normalizeRequestTraceFilters(base.filters),
    date_range: base.date_range,
    operation: normalizeTraceOperation(searchParams.get("operation")),
    has_results: normalizeHasResults(searchParams.get("hasResults")),
    page: normalizeTracePage(base.page, base.page_size),
    page_size: normalizePageSize(base.page_size),
  };
}

export function normalizeRequestTraceFilters(
  filters: ExplorerFilter[],
): ExplorerFilter[] {
  const normalized: ExplorerFilter[] = [];
  for (const filter of filters) {
    if (
      !ENTITY_FIELDS.has(filter.field) ||
      filter.operator !== "equals" ||
      typeof filter.value !== "string" ||
      filter.value.trim() === ""
    ) {
      continue;
    }
    const exact = {
      ...filter,
      operator: "equals" as const,
      value: filter.value.trim(),
    };
    const duplicateIndex = normalized.findIndex(
      (candidate) => candidate.field === exact.field,
    );
    if (duplicateIndex >= 0) {
      normalized.splice(duplicateIndex, 1);
    }
    normalized.push(exact);
  }
  return normalized;
}

export function requestTraceQueryPayload(
  query: RequestTraceQueryState,
): SidecarTraceQuery {
  const entityFilters: SidecarTraceQuery["entity_filters"] = {};
  for (const filter of normalizeRequestTraceFilters(query.filters)) {
    if (typeof filter.value !== "string") continue;
    if (filter.field === "user_id") entityFilters.user_id = filter.value;
    if (filter.field === "agent_id") entityFilters.agent_id = filter.value;
    if (filter.field === "app_id") entityFilters.app_id = filter.value;
    if (filter.field === "run_id") entityFilters.run_id = filter.value;
  }
  return {
    operation: query.operation,
    statuses: [],
    has_results: query.has_results,
    date_range: query.date_range,
    entity_filters: entityFilters,
    page: normalizeTracePage(query.page, query.page_size),
    page_size: normalizePageSize(query.page_size),
  };
}

export function resetRequestTraceQueryPage(
  query: RequestTraceQueryState,
): RequestTraceQueryState {
  return { ...query, page: 1 };
}

export function setRequestTraceOperation(
  query: RequestTraceQueryState,
  operation: SidecarTraceQuery["operation"],
): RequestTraceQueryState {
  return resetRequestTraceQueryPage({ ...query, operation });
}

export function toggleRequestTraceHasResults(
  query: RequestTraceQueryState,
): RequestTraceQueryState {
  return resetRequestTraceQueryPage({
    ...query,
    has_results: query.has_results === true ? null : true,
  });
}

export function normalizeTracePage(
  value: unknown,
  pageSize: unknown = DEFAULT_PAGE_SIZE,
): number {
  if (typeof value !== "number" || !Number.isSafeInteger(value) || value < 1) {
    return 1;
  }
  const size = normalizePageSize(pageSize);
  return Math.min(value, Math.ceil(EVENT_SCAN_HORIZON / size));
}

export function setTraceRequestIdInUrl(
  current: URLSearchParams,
  requestId: string,
): URLSearchParams {
  const next = new URLSearchParams(current.toString());
  next.set("requestId", requestId);
  return next;
}

export function closeTraceRequestUrl(
  current: URLSearchParams,
): URLSearchParams {
  const next = new URLSearchParams(current.toString());
  next.delete("requestId");
  return next;
}

export function writeTraceControlUrl(
  current: URLSearchParams,
  operation: SidecarTraceQuery["operation"],
  hasResults: boolean | null,
): URLSearchParams {
  const next = new URLSearchParams(current.toString());
  writeOptional(next, "operation", operation);
  writeOptional(
    next,
    "hasResults",
    hasResults === null ? null : String(hasResults),
  );
  return next;
}

export function nextTraceRequestGeneration(current: number): number {
  return current + 1;
}

export function isCurrentTraceListRequest(
  requestGeneration: number,
  currentGeneration: number,
  mounted: boolean,
): boolean {
  return mounted && requestGeneration === currentGeneration;
}

export function beginTraceDetailRequest(
  currentGeneration: number,
  targetId: string,
): TraceDetailRequest {
  return {
    generation: nextTraceRequestGeneration(currentGeneration),
    targetId,
  };
}

export function canApplyTraceDetailRequest(
  request: TraceDetailRequest,
  currentGeneration: number,
  activeRequestId: string | null,
  mounted: boolean,
): boolean {
  return (
    mounted &&
    request.generation === currentGeneration &&
    request.targetId === activeRequestId
  );
}

function normalizeTraceOperation(
  value: string | null,
): SidecarTraceQuery["operation"] {
  return value === "ADD" || value === "SEARCH" || value === "GET_ALL"
    ? value
    : null;
}

function normalizeHasResults(value: string | null): boolean | null {
  if (value === "true") return true;
  return null;
}

function normalizePageSize(value: unknown): number {
  return typeof value === "number" &&
    Number.isSafeInteger(value) &&
    value >= 1 &&
    value <= 100
    ? value
    : DEFAULT_PAGE_SIZE;
}

function writeOptional(
  params: URLSearchParams,
  key: string,
  value: string | null,
) {
  if (value === null) {
    params.delete(key);
  } else {
    params.set(key, value);
  }
}
