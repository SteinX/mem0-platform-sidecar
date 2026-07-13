import type {
  ExplorerDateRange,
  ExplorerField,
  ExplorerFilter,
  ExplorerMatch,
  ExplorerOperator,
} from "@/types/dashboard-explorer";
import { normalizeExplorerFilters } from "@/utils/explorer-query-state";

export type ExplorerCalendarRange = {
  from: Date | undefined;
  to?: Date;
};

export type FilterBuilderDraft = {
  open: boolean;
  match: ExplorerMatch;
  filters: ExplorerFilter[];
};

export type EntityBadgeItem = {
  field: "user_id" | "agent_id" | "app_id" | "run_id";
  label: "User" | "Agent" | "App" | "Run";
  value: string;
};

type EntityBadgeSource = {
  userId?: string | null;
  agentId?: string | null;
  appId?: string | null;
  runId?: string | null;
};

type AppliedFilterDraft = {
  draft: FilterBuilderDraft;
  match: ExplorerMatch;
  filters: ExplorerFilter[];
};

type RemovedFilterDraft = {
  draft: FilterBuilderDraft;
  filters: ExplorerFilter[];
};

const SCALAR_OPERATORS: ExplorerOperator[] = ["equals", "not_equals", "in"];

export function formatDateRangeLabel(value: ExplorerDateRange): string {
  if (value.from === null && value.to === null) {
    return "All time";
  }
  const from = value.from === null ? "Start" : formatIsoDate(value.from);
  const to = value.to === null ? "Now" : formatIsoDate(value.to);
  return `${from} – ${to}`;
}

export function isoRangeToCalendarRange(
  value: ExplorerDateRange,
): ExplorerCalendarRange | undefined {
  const from = isoToCalendarDate(value.from);
  const to = isoToCalendarDate(value.to);
  return from === undefined && to === undefined ? undefined : { from, to };
}

export function calendarRangeToUtcRange(
  range: ExplorerCalendarRange,
): ExplorerDateRange | null {
  if (range.from === undefined || range.to === undefined) {
    return null;
  }
  const from = Date.UTC(
    range.from.getFullYear(),
    range.from.getMonth(),
    range.from.getDate(),
  );
  const to = Date.UTC(
    range.to.getFullYear(),
    range.to.getMonth(),
    range.to.getDate(),
    23,
    59,
    59,
    999,
  );
  return { from: new Date(from).toISOString(), to: new Date(to).toISOString() };
}

export function createFilterBuilderDraft(
  match: ExplorerMatch,
  filters: ExplorerFilter[],
): FilterBuilderDraft {
  return { open: false, match, filters: cloneFiltersWithUniqueIds(filters) };
}

export function openFilterBuilderDraft(
  match: ExplorerMatch,
  filters: ExplorerFilter[],
): FilterBuilderDraft {
  return { ...createFilterBuilderDraft(match, filters), open: true };
}

export function cancelFilterBuilderDraft(
  draft: FilterBuilderDraft,
): FilterBuilderDraft {
  return { ...draft, open: false };
}

export function applyFilterBuilderDraft(
  draft: FilterBuilderDraft,
): AppliedFilterDraft {
  const filters = normalizeExplorerFilters(draft.filters);
  return {
    draft: { ...draft, open: false, filters },
    match: draft.match,
    filters,
  };
}

export function removeAllFilterBuilderDraft(
  draft: FilterBuilderDraft,
): RemovedFilterDraft {
  const filters: ExplorerFilter[] = [];
  return { draft: { ...draft, open: false, filters }, filters };
}

export function changeExplorerFilterField(
  filter: ExplorerFilter,
  field: ExplorerField,
): ExplorerFilter {
  const operator: ExplorerOperator = field === "metadata" ? "contains" : "equals";
  return { ...filter, field, operator, value: emptyValueFor(field, operator) };
}

export function changeExplorerFilterOperator(
  filter: ExplorerFilter,
  requested: ExplorerOperator,
): ExplorerFilter {
  const compatible = filterOperatorsForField(filter.field);
  const operator = compatible.includes(requested) ? requested : compatible[0];
  return {
    ...filter,
    operator,
    value: emptyValueFor(filter.field, operator),
  };
}

export function filterOperatorsForField(field: ExplorerField): ExplorerOperator[] {
  return field === "metadata" ? ["contains"] : [...SCALAR_OPERATORS];
}

export function parseCommaSeparatedFilterValues(value: string): string[] {
  return value.split(",").map((item) => item.trim()).filter(Boolean);
}

export function toggleFilterValue(
  values: string[],
  value: string,
  checked: boolean,
): string[] {
  if (checked) {
    return values.includes(value) ? values : [...values, value];
  }
  return values.filter((candidate) => candidate !== value);
}

export function createEntityBadgeItems(source: EntityBadgeSource): EntityBadgeItem[] {
  const candidates: Array<Omit<EntityBadgeItem, "value"> & {
    value: string | null | undefined;
  }> = [
    { field: "user_id", label: "User", value: source.userId },
    { field: "agent_id", label: "Agent", value: source.agentId },
    { field: "app_id", label: "App", value: source.appId },
    { field: "run_id", label: "Run", value: source.runId },
  ];
  return candidates.filter(
    (identity): identity is EntityBadgeItem => (
      typeof identity.value === "string" && identity.value.trim() !== ""
    ),
  );
}

export function entityBadgeClickPayload(
  identity: EntityBadgeItem,
): { field: EntityBadgeItem["field"]; value: string } {
  return { field: identity.field, value: identity.value };
}

export function truncateIdentity(value: string): string {
  return value.length <= 18 ? value : `${value.slice(0, 9)}…${value.slice(-6)}`;
}

function cloneFiltersWithUniqueIds(filters: ExplorerFilter[]): ExplorerFilter[] {
  const reserved = new Set(filters.map(({ id }) => id));
  const used = new Set<string>();
  return filters.map((filter, index) => {
    const id = uniqueDraftId(filter.id, index, reserved, used);
    used.add(id);
    return { ...filter, id, value: cloneFilterValue(filter.value) };
  });
}

function uniqueDraftId(
  id: string,
  index: number,
  reserved: Set<string>,
  used: Set<string>,
): string {
  if (id !== "" && !used.has(id)) {
    return id;
  }
  const base = id === "" ? `filter-${index + 1}` : id;
  let occurrence = 2;
  let candidate = `${base}-duplicate-${occurrence}`;
  while (reserved.has(candidate) || used.has(candidate)) {
    occurrence += 1;
    candidate = `${base}-duplicate-${occurrence}`;
  }
  return candidate;
}

function cloneFilterValue(value: ExplorerFilter["value"]): ExplorerFilter["value"] {
  if (Array.isArray(value)) {
    return [...value];
  }
  return typeof value === "object" ? { ...value } : value;
}

function emptyValueFor(
  field: ExplorerField,
  operator: ExplorerOperator,
): ExplorerFilter["value"] {
  if (field === "metadata") {
    return { key: "", value: "" };
  }
  return operator === "in" ? [] : "";
}

function isoToCalendarDate(value: string | null): Date | undefined {
  if (value === null) {
    return undefined;
  }
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) {
    return undefined;
  }
  return new Date(date.getUTCFullYear(), date.getUTCMonth(), date.getUTCDate());
}

function formatIsoDate(value: string): string {
  const date = new Date(value);
  return Number.isFinite(date.getTime()) ? date.toISOString().slice(0, 10) : value;
}
