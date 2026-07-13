"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ChevronRight, RefreshCw } from "lucide-react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { Bar, BarChart, ResponsiveContainer, Tooltip, XAxis } from "recharts";

import { DataTable } from "@/components/shared/data-table";
import { DateRangeFilter } from "@/components/self-hosted/explorer/date-range-filter";
import { EntityBadges } from "@/components/self-hosted/explorer/entity-badges";
import {
  FilterBuilder,
  type ExplorerFilterFieldOption,
} from "@/components/self-hosted/explorer/filter-builder";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Pagination,
  PaginationContent,
  PaginationItem,
  PaginationNext,
  PaginationPrevious,
} from "@/components/ui/pagination";
import type {
  ExplorerDateRange,
  ExplorerFilter,
  ExplorerMatch,
  ExplorerQueryPayload,
} from "@/types/dashboard-explorer";
import type { SidecarTrace, SidecarTracePage } from "@/types/sidecar";
import {
  createExplorerFilter,
  readExplorerUrlState,
  writeExplorerUrlState,
} from "@/utils/explorer-query-state";
import { sidecarQuery } from "@/utils/sidecar-api";
import {
  closeTraceRequestUrl,
  isCurrentTraceListRequest,
  nextTraceRequestGeneration,
  normalizeRequestTraceFilters,
  normalizeRequestTraceQueryState,
  normalizeTracePage,
  requestTraceQueryPayload,
  resetRequestTraceQueryPage,
  setRequestTraceOperation,
  setTraceRequestIdInUrl,
  toggleRequestTraceHasResults,
  writeTraceControlUrl,
  type RequestTraceQueryState,
} from "@/utils/request-trace-state";

import { RequestTraceDrawer } from "./request-trace-drawer";

type TraceOperation = RequestTraceQueryState["operation"];

type TraceColumn = {
  key: keyof SidecarTrace;
  label: string;
  width?: number;
  align?: "left" | "center" | "right";
  render?: (
    value: SidecarTrace[keyof SidecarTrace],
    row: SidecarTrace,
  ) => React.ReactNode;
};

const DEFAULT_QUERY: RequestTraceQueryState = {
  match: "all",
  filters: [],
  date_range: { from: null, to: null },
  operation: null,
  has_results: null,
  page: 1,
  page_size: 20,
};

const FILTER_FIELDS: ExplorerFilterFieldOption[] = [
  { value: "user_id", label: "User ID", operators: ["equals"] },
  { value: "agent_id", label: "Agent ID", operators: ["equals"] },
  { value: "app_id", label: "App ID", operators: ["equals"] },
  { value: "run_id", label: "Run ID", operators: ["equals"] },
];

const OPERATION_CONTROLS: Array<{
  label: string;
  operation: TraceOperation;
}> = [
  { label: "Overview", operation: null },
  { label: "ADD", operation: "ADD" },
  { label: "SEARCH", operation: "SEARCH" },
  { label: "GET ALL", operation: "GET_ALL" },
];

export default function RequestsPage() {
  const pathname = usePathname();
  const router = useRouter();
  const searchParams = useSearchParams();
  const search = searchParams.toString();
  const [query, setQuery] = useState<RequestTraceQueryState>(DEFAULT_QUERY);
  const [hydrated, setHydrated] = useState(false);
  const [pageData, setPageData] = useState<SidecarTracePage | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [isRefreshing, setIsRefreshing] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [refreshVersion, setRefreshVersion] = useState(0);
  const requestGeneration = useRef(0);
  const pageDataRef = useRef<SidecarTracePage | null>(null);
  const mountedRef = useRef(true);

  const requestId = normalizeRequestId(searchParams.get("requestId"));

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      requestGeneration.current = nextTraceRequestGeneration(
        requestGeneration.current,
      );
    };
  }, []);

  useEffect(() => {
    const currentParams = new URLSearchParams(search);
    const nextQuery = readRequestUrlState(currentParams);
    const canonicalParams = writeRequestUrlState(currentParams, nextQuery);
    const canonicalSearch = canonicalParams.toString();
    if (canonicalSearch !== search) {
      router.replace(
        canonicalSearch ? `${pathname}?${canonicalSearch}` : pathname,
      );
    }
    setQuery((current) =>
      requestQueriesEqual(current, nextQuery) ? current : nextQuery,
    );
    setHydrated(true);
  }, [pathname, router, search]);

  const replaceParams = useCallback(
    (next: URLSearchParams) => {
      const value = next.toString();
      router.replace(value ? `${pathname}?${value}` : pathname);
    },
    [pathname, router],
  );

  const writeQuery = useCallback(
    (next: RequestTraceQueryState) => {
      setQuery(next);
      replaceParams(writeRequestUrlState(new URLSearchParams(search), next));
    },
    [replaceParams, search],
  );

  useEffect(() => {
    if (!hydrated) {
      return;
    }
    const controller = new AbortController();
    const generation = nextTraceRequestGeneration(requestGeneration.current);
    requestGeneration.current = generation;
    if (pageDataRef.current === null) {
      setIsLoading(true);
    } else {
      setIsRefreshing(true);
    }
    setLoadError(null);

    void sidecarQuery<SidecarTracePage>(
      "/v1/events/query",
      requestTraceQueryPayload(query),
      {
        signal: controller.signal,
      },
    )
      .then((response) => {
        if (
          !controller.signal.aborted &&
          isCurrentTraceListRequest(
            generation,
            requestGeneration.current,
            mountedRef.current,
          )
        ) {
          pageDataRef.current = response;
          setPageData(response);
        }
      })
      .catch((error: unknown) => {
        if (
          !controller.signal.aborted &&
          isCurrentTraceListRequest(
            generation,
            requestGeneration.current,
            mountedRef.current,
          ) &&
          !isAbortError(error)
        ) {
          setLoadError(error instanceof Error ? error.message : String(error));
        }
      })
      .finally(() => {
        if (
          !controller.signal.aborted &&
          isCurrentTraceListRequest(
            generation,
            requestGeneration.current,
            mountedRef.current,
          )
        ) {
          setIsLoading(false);
          setIsRefreshing(false);
        }
      });

    return () => {
      controller.abort();
    };
  }, [hydrated, query, refreshVersion]);

  const refresh = useCallback(() => {
    setRefreshVersion((value) => value + 1);
  }, []);

  const applyCriteria = useCallback(
    (_match: ExplorerMatch, filters: ExplorerFilter[]) => {
      writeQuery(
        resetRequestTraceQueryPage({
          ...query,
          match: "all",
          filters: normalizeRequestTraceFilters(filters),
        }),
      );
    },
    [query, writeQuery],
  );

  const changeDateRange = useCallback(
    (dateRange: ExplorerDateRange) => {
      writeQuery(
        resetRequestTraceQueryPage({ ...query, date_range: dateRange }),
      );
    },
    [query, writeQuery],
  );

  const addIdentityFilter = useCallback(
    (identity: {
      field: "user_id" | "agent_id" | "app_id" | "run_id";
      value: string;
    }) => {
      const filter = createExplorerFilter({
        field: identity.field,
        operator: "equals",
        value: identity.value,
      });
      applyCriteria("all", [...query.filters, filter]);
    },
    [applyCriteria, query.filters],
  );

  const selectOperation = useCallback(
    (operation: TraceOperation) => {
      writeQuery(setRequestTraceOperation(query, operation));
    },
    [query, writeQuery],
  );

  const toggleHasResults = useCallback(() => {
    writeQuery(toggleRequestTraceHasResults(query));
  }, [query, writeQuery]);

  const setDrawerRequestId = useCallback(
    (id: string | null) => {
      const current = new URLSearchParams(search);
      const next =
        id === null
          ? closeTraceRequestUrl(current)
          : setTraceRequestIdInUrl(current, id);
      replaceParams(next);
    },
    [replaceParams, search],
  );

  const columns = useMemo<TraceColumn[]>(
    () => [
      {
        key: "requested_at",
        label: "Time",
        width: 16,
        render: (value) => (
          <TraceTime value={typeof value === "string" ? value : null} />
        ),
      },
      {
        key: "display_operation",
        label: "Type",
        width: 13,
        render: (_value, row) => (
          <OperationBadge operation={row.display_operation} />
        ),
      },
      {
        key: "entities",
        label: "Entities",
        width: 24,
        render: (_value, row) => (
          <div onClick={(event) => event.stopPropagation()}>
            <TraceEntities trace={row} onBadgeClick={addIdentityFilter} />
          </div>
        ),
      },
      {
        key: "request",
        label: "Event",
        width: 29,
        render: (_value, row) => (
          <TraceEventButton
            trace={row}
            onOpen={() => setDrawerRequestId(row.id)}
          />
        ),
      },
      {
        key: "latency_ms",
        label: "Latency",
        width: 10,
        render: (_value, row) => formatLatency(row.latency_ms),
      },
      {
        key: "status",
        label: "Status",
        width: 13,
        render: (_value, row) => <StatusBadge status={row.status} />,
      },
    ],
    [addIdentityFilter, setDrawerRequestId],
  );

  const rows = pageData?.results ?? [];
  const hasInitialError = loadError !== null && pageData === null;
  const hasNextPage =
    pageData?.has_more === true &&
    normalizeTracePage(query.page + 1, query.page_size) > query.page;

  return (
    <div className="min-w-0 space-y-5">
      <div className="flex min-w-0 flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div className="min-w-0 space-y-1">
          <h1 className="font-fustat text-xl font-semibold">Requests</h1>
          <p className="break-words text-sm text-onSurface-default-secondary">
            Inspect scoped memory operations, latency, results, and sanitized
            payloads.
          </p>
        </div>
        <Button
          type="button"
          variant="outline"
          disabled={isLoading || isRefreshing}
          onClick={refresh}
        >
          <RefreshCw
            className={`mr-2 size-4 ${isRefreshing ? "animate-spin" : ""}`}
          />
          {isRefreshing ? "Refreshing" : "Refresh"}
        </Button>
      </div>

      <div
        className="flex flex-wrap items-center gap-2"
        aria-label="Request operation filters"
      >
        {OPERATION_CONTROLS.map((control) => {
          const active = query.operation === control.operation;
          return (
            <Button
              key={control.label}
              type="button"
              size="sm"
              variant={active ? "default" : "outline"}
              aria-pressed={active}
              onClick={() => selectOperation(control.operation)}
            >
              {control.label}
            </Button>
          );
        })}
      </div>

      <div
        className="flex flex-wrap items-center gap-2"
        aria-label="Has results filter"
      >
        <Button
          type="button"
          size="sm"
          variant={query.has_results === true ? "default" : "outline"}
          aria-pressed={query.has_results === true}
          onClick={toggleHasResults}
        >
          Has Results
        </Button>
      </div>

      <div className="flex flex-wrap items-center gap-2">
        <DateRangeFilter value={query.date_range} onChange={changeDateRange} />
        <FilterBuilder
          match="all"
          filters={query.filters}
          fields={FILTER_FIELDS}
          allowAnyMatch={false}
          onApply={applyCriteria}
          onRemoveAll={(filters) => applyCriteria("all", filters)}
        />
        <span className="text-xs text-onSurface-default-tertiary">
          Entity filters use exact ID matches.
        </span>
      </div>

      {loadError !== null ? (
        <div
          role="alert"
          className="flex flex-wrap items-center justify-between gap-3 border-y border-memBorder-primary py-3"
        >
          <span className="break-all text-sm text-onSurface-danger-primary">
            {pageData
              ? `Could not refresh requests: ${loadError}`
              : `Could not load requests: ${loadError}`}
          </span>
          <Button type="button" size="sm" variant="outline" onClick={refresh}>
            Retry
          </Button>
        </div>
      ) : null}

      {pageData !== null ? (
        <section
          aria-labelledby="request-timeline-heading"
          className="space-y-3"
        >
          <div className="flex items-baseline justify-between gap-3">
            <h2 id="request-timeline-heading" className="font-semibold">
              Request timeline
            </h2>
            <span className="text-sm text-onSurface-default-secondary">
              {pageData.total} total
            </span>
          </div>
          {pageData.timeline.length > 0 ? (
            <>
              <div className="h-40 min-w-0 rounded-md border border-memBorder-primary p-3">
                <ResponsiveContainer width="100%" height="100%">
                  <BarChart data={pageData.timeline}>
                    <XAxis
                      dataKey="timestamp"
                      tickFormatter={formatTimelineTick}
                      minTickGap={28}
                    />
                    <Tooltip
                      labelFormatter={(value) =>
                        formatTraceTimestamp(String(value))
                      }
                    />
                    <Bar
                      dataKey="count"
                      fill="hsl(var(--primary))"
                      radius={[4, 4, 0, 0]}
                    />
                  </BarChart>
                </ResponsiveContainer>
              </div>
              <p
                role="status"
                aria-label="Request timeline summary"
                className="text-xs text-onSurface-default-secondary"
              >
                {timelineSummary(pageData)}
              </p>
            </>
          ) : (
            <p className="rounded-md border border-memBorder-primary p-6 text-center text-sm text-onSurface-default-secondary">
              No request activity for this range.
            </p>
          )}
        </section>
      ) : null}

      {isLoading && pageData === null ? (
        <div
          role="status"
          className="py-16 text-center text-sm text-onSurface-default-secondary"
        >
          Loading requests...
        </div>
      ) : hasInitialError ? null : rows.length === 0 ? (
        <div className="py-16 text-center text-sm text-onSurface-default-secondary">
          No requests found.
        </div>
      ) : (
        <>
          <div className="hidden min-w-0 overflow-hidden border-y border-memBorder-primary md:block">
            <DataTable
              data={rows}
              columns={columns}
              getRowKey={(row) => row.id}
              onRowClick={(row) => setDrawerRequestId(row.id)}
            />
          </div>
          <div className="space-y-2 md:hidden">
            {rows.map((trace) => (
              <button
                key={trace.id}
                type="button"
                className="w-full min-w-0 space-y-3 rounded-md border border-memBorder-primary p-4 text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                onClick={() => setDrawerRequestId(trace.id)}
              >
                <div className="flex items-start justify-between gap-2">
                  <div className="flex flex-wrap items-center gap-2">
                    <OperationBadge operation={trace.display_operation} />
                    <StatusBadge status={trace.status} />
                  </div>
                  <ChevronRight className="size-4 shrink-0" />
                </div>
                <p className="whitespace-normal break-words text-sm">
                  {traceEventLabel(trace)}
                </p>
                <TraceEntities trace={trace} />
                <div className="flex flex-wrap items-center justify-between gap-2 text-xs text-onSurface-default-secondary">
                  <TraceTime value={trace.requested_at} />
                  <span>{formatLatency(trace.latency_ms)}</span>
                </div>
              </button>
            ))}
          </div>
        </>
      )}

      {pageData && (query.page > 1 || hasNextPage) ? (
        <Pagination>
          <PaginationContent>
            <PaginationItem>
              <PaginationPrevious
                href="#"
                isDisabled={query.page <= 1 || isRefreshing}
                aria-disabled={query.page <= 1 || isRefreshing}
                tabIndex={query.page <= 1 || isRefreshing ? -1 : undefined}
                onClick={(event) => {
                  event.preventDefault();
                  writeQuery({ ...query, page: Math.max(1, query.page - 1) });
                }}
              />
            </PaginationItem>
            <PaginationItem>
              <span className="px-3 text-sm" aria-live="polite">
                Page {query.page}
              </span>
            </PaginationItem>
            <PaginationItem>
              <PaginationNext
                href="#"
                isDisabled={!hasNextPage || isRefreshing}
                aria-disabled={!hasNextPage || isRefreshing}
                tabIndex={!hasNextPage || isRefreshing ? -1 : undefined}
                onClick={(event) => {
                  event.preventDefault();
                  writeQuery({
                    ...query,
                    page: normalizeTracePage(query.page + 1, query.page_size),
                  });
                }}
              />
            </PaginationItem>
          </PaginationContent>
        </Pagination>
      ) : null}

      <RequestTraceDrawer
        requestId={requestId}
        onRequestIdChange={setDrawerRequestId}
      />
    </div>
  );
}

function readRequestUrlState(
  searchParams: URLSearchParams,
): RequestTraceQueryState {
  const shared = readExplorerUrlState(searchParams);
  return normalizeRequestTraceQueryState(shared, searchParams);
}

function writeRequestUrlState(
  current: URLSearchParams,
  query: RequestTraceQueryState,
): URLSearchParams {
  const sharedQuery: ExplorerQueryPayload = {
    match: query.match,
    filters: query.filters,
    date_range: query.date_range,
    page: query.page,
    page_size: query.page_size,
    sort: "created_at_desc",
  };
  const next = writeExplorerUrlState(current, sharedQuery);
  return writeTraceControlUrl(next, query.operation, query.has_results);
}

function requestQueriesEqual(
  left: RequestTraceQueryState,
  right: RequestTraceQueryState,
) {
  return JSON.stringify(left) === JSON.stringify(right);
}

function normalizeRequestId(value: unknown): string | null {
  return typeof value === "string" && value.trim() !== "" ? value.trim() : null;
}

function isAbortError(error: unknown): boolean {
  return (
    error !== null &&
    typeof error === "object" &&
    "name" in error &&
    error.name === "AbortError"
  );
}

function traceEntityId(
  trace: SidecarTrace,
  type: "user" | "agent" | "app" | "run",
) {
  return trace.entities.find((entity) => entity.type === type)?.id ?? null;
}

function TraceEntities({
  trace,
  onBadgeClick,
}: {
  trace: SidecarTrace;
  onBadgeClick?: (identity: {
    field: "user_id" | "agent_id" | "app_id" | "run_id";
    value: string;
  }) => void;
}) {
  return (
    <EntityBadges
      userId={traceEntityId(trace, "user")}
      agentId={traceEntityId(trace, "agent")}
      appId={traceEntityId(trace, "app")}
      runId={traceEntityId(trace, "run")}
      onBadgeClick={onBadgeClick}
    />
  );
}

function TraceEventButton({
  trace,
  onOpen,
}: {
  trace: SidecarTrace;
  onOpen: () => void;
}) {
  const label = traceEventLabel(trace);
  return (
    <button
      type="button"
      className="line-clamp-2 w-full rounded-sm text-left whitespace-normal break-words hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
      title={label}
      aria-label={`Open request ${trace.id}: ${label}`}
      onClick={(event) => {
        event.stopPropagation();
        onOpen();
      }}
    >
      {label}
    </button>
  );
}

function OperationBadge({
  operation,
}: {
  operation: SidecarTrace["display_operation"];
}) {
  return (
    <Badge variant="outline" className="whitespace-nowrap">
      {operation}
    </Badge>
  );
}

function StatusBadge({ status }: { status: SidecarTrace["status"] }) {
  const className =
    status === "FAILED"
      ? "border-rose-300 text-rose-700 dark:text-rose-300"
      : status === "SUCCEEDED"
        ? "border-emerald-300 text-emerald-700 dark:text-emerald-300"
        : "border-amber-300 text-amber-700 dark:text-amber-300";
  return (
    <Badge variant="outline" className={className}>
      {status}
    </Badge>
  );
}

function traceEventLabel(trace: SidecarTrace): string {
  const query = trace.request.query;
  if (trace.display_operation === "SEARCH" && typeof query === "string") {
    return query;
  }
  if (trace.display_operation === "ADD") {
    return "Add memory";
  }
  if (trace.display_operation === "GET ALL") {
    return "Get all memories";
  }
  return trace.operation || "Memory event";
}

function formatLatency(value: number | null): string {
  return value === null || !Number.isFinite(value)
    ? "--"
    : `${value.toFixed(2)} ms`;
}

function TraceTime({ value }: { value: string | null }) {
  return value === null ? (
    <span className="text-onSurface-default-tertiary">Unknown</span>
  ) : (
    <time dateTime={value} title={formatTraceTimestamp(value)}>
      {formatTraceTimestamp(value)}
    </time>
  );
}

function formatTraceTimestamp(value: string): string {
  const date = new Date(value);
  return Number.isFinite(date.getTime())
    ? date.toISOString().replace("T", " ").replace(".000Z", " UTC")
    : value;
}

function formatTimelineTick(value: string): string {
  const date = new Date(value);
  return Number.isFinite(date.getTime())
    ? date.toISOString().slice(5, 16).replace("T", " ")
    : value;
}

function timelineSummary(page: SidecarTracePage): string {
  const visible = page.timeline.reduce(
    (total, bucket) => total + bucket.count,
    0,
  );
  return `${visible} requests across ${page.timeline.length} timeline ${page.timeline.length === 1 ? "bucket" : "buckets"}.`;
}
