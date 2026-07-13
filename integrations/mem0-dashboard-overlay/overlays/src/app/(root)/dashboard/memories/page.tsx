"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ChevronRight, RefreshCw } from "lucide-react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";

import { DataTable } from "@/components/shared/data-table";
import { DateRangeFilter } from "@/components/self-hosted/explorer/date-range-filter";
import { EntityBadges } from "@/components/self-hosted/explorer/entity-badges";
import {
  FilterBuilder,
  type ExplorerFilterFieldOption,
} from "@/components/self-hosted/explorer/filter-builder";
import { Button } from "@/components/ui/button";
import { CategoriesDisplay } from "@/components/ui/categories-display";
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
import type { SidecarMemory, SidecarMemoryPage } from "@/types/sidecar";
import { createExplorerFilter, readExplorerUrlState, writeExplorerUrlState } from "@/utils/explorer-query-state";
import {
  closeMemoryUrl,
  isCurrentMemoryRequest,
  memoryDeleteNavigation,
  memoryQueriesEqual,
  memoryQueryPayload,
  nextMemoryRequestGeneration,
  resetMemoryQueryPage,
  setMemoryIdInUrl,
} from "@/utils/memory-explorer-state";
import { sidecarQuery } from "@/utils/sidecar-api";

import { MemoryDetailDrawer } from "./memory-detail-drawer";

const DEFAULT_QUERY: ExplorerQueryPayload = {
  match: "all",
  filters: [],
  date_range: { from: null, to: null },
  page: 1,
  page_size: 20,
  sort: "created_at_desc",
};

const FILTER_FIELDS: ExplorerFilterFieldOption[] = [
  { value: "user_id", label: "User ID" },
  { value: "agent_id", label: "Agent ID" },
  { value: "app_id", label: "App ID" },
  { value: "run_id", label: "Run ID" },
  { value: "memory_id", label: "Memory ID" },
  { value: "entity_type", label: "Entity type", options: [
    { value: "user", label: "User" },
    { value: "agent", label: "Agent" },
    { value: "app", label: "App" },
    { value: "run", label: "Run" },
  ] },
  { value: "category", label: "Category" },
  { value: "metadata", label: "Metadata" },
];

type MemoryColumn = {
  key: keyof SidecarMemory;
  label: string;
  width?: number;
  className?: string;
  align?: "left" | "center" | "right";
  render?: (value: SidecarMemory[keyof SidecarMemory], row: SidecarMemory) => React.ReactNode;
};

export default function MemoriesPage() {
  const pathname = usePathname();
  const router = useRouter();
  const searchParams = useSearchParams();
  const search = searchParams.toString();
  const [query, setQuery] = useState<ExplorerQueryPayload>(DEFAULT_QUERY);
  const [hydrated, setHydrated] = useState(false);
  const [pageData, setPageData] = useState<SidecarMemoryPage | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [isRefreshing, setIsRefreshing] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [refreshVersion, setRefreshVersion] = useState(0);
  const requestGeneration = useRef(0);

  const memoryId = searchParams.get("memoryId");

  useEffect(() => {
    const urlState = readExplorerUrlState(new URLSearchParams(search));
    const nextQuery: ExplorerQueryPayload = {
      match: urlState.match,
      filters: urlState.filters,
      date_range: urlState.date_range,
      page: urlState.page,
      page_size: urlState.page_size,
      sort: urlState.sort,
    };
    setQuery((current) => memoryQueriesEqual(current, nextQuery) ? current : nextQuery);
    setHydrated(true);
  }, [search]);

  const replaceParams = useCallback((next: URLSearchParams) => {
    const value = next.toString();
    router.replace(value ? `${pathname}?${value}` : pathname);
  }, [pathname, router]);

  const writeQuery = useCallback((next: ExplorerQueryPayload) => {
    setQuery(next);
    replaceParams(writeExplorerUrlState(new URLSearchParams(search), next));
  }, [replaceParams, search]);

  const loadMemories = useCallback(async () => {
    const generation = nextMemoryRequestGeneration(requestGeneration.current);
    requestGeneration.current = generation;
    if (pageData === null) {
      setIsLoading(true);
    } else {
      setIsRefreshing(true);
    }
    setLoadError(null);
    try {
      const response = await sidecarQuery<SidecarMemoryPage>(
        "/v1/memories/query",
        memoryQueryPayload(query),
      );
      if (isCurrentMemoryRequest(generation, requestGeneration.current)) {
        setPageData(response);
      }
    } catch (error) {
      if (isCurrentMemoryRequest(generation, requestGeneration.current)) {
        setLoadError(error instanceof Error ? error.message : String(error));
      }
    } finally {
      if (isCurrentMemoryRequest(generation, requestGeneration.current)) {
        setIsLoading(false);
        setIsRefreshing(false);
      }
    }
  }, [pageData, query]);

  useEffect(() => {
    if (hydrated) {
      void loadMemories();
    }
  // pageData is deliberately excluded so preserving old data does not refetch forever.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [hydrated, query, refreshVersion]);

  const refresh = useCallback(() => setRefreshVersion((value) => value + 1), []);

  const applyCriteria = (match: ExplorerMatch, filters: ExplorerFilter[]) => {
    writeQuery(resetMemoryQueryPage({ ...query, match, filters }));
  };

  const changeDateRange = (dateRange: ExplorerDateRange) => {
    writeQuery(resetMemoryQueryPage({ ...query, date_range: dateRange }));
  };

  const addIdentityFilter = (identity: { field: "user_id" | "agent_id" | "app_id" | "run_id"; value: string }) => {
    const filter = createExplorerFilter({
      field: identity.field,
      operator: "equals",
      value: identity.value,
    });
    applyCriteria(query.match, [...query.filters, filter]);
  };

  const openMemory = (id: string) => {
    replaceParams(setMemoryIdInUrl(new URLSearchParams(search), id));
  };

  const setDrawerMemoryId = (id: string | null) => {
    const current = new URLSearchParams(search);
    replaceParams(id === null ? closeMemoryUrl(current) : setMemoryIdInUrl(current, id));
  };

  const handleDeleted = (deletedMemoryId: string) => {
    const deletedMemoryIsOnPage = pageData?.results.some(
      (memory) => memory.id === deletedMemoryId,
    ) ?? false;
    const navigation = memoryDeleteNavigation(
      new URLSearchParams(search),
      query.page,
      pageData?.results.length ?? 0,
      deletedMemoryIsOnPage,
    );
    const nextQuery = { ...query, page: navigation.page };
    setQuery(nextQuery);
    replaceParams(writeExplorerUrlState(navigation.searchParams, nextQuery));
    if (navigation.page === query.page) {
      refresh();
    }
  };

  const columns = useMemo<MemoryColumn[]>(() => [
    {
      key: "created_at",
      label: "Time",
      width: 18,
      render: (value) => <MemoryTime value={typeof value === "string" ? value : null} />,
    },
    {
      key: "user_id",
      label: "Entities",
      width: 25,
      render: (_value, row) => (
        <div onClick={(event) => event.stopPropagation()}>
          <EntityBadges
            userId={row.user_id}
            agentId={row.agent_id}
            appId={row.app_id}
            runId={row.run_id}
            onBadgeClick={addIdentityFilter}
          />
        </div>
      ),
    },
    {
      key: "memory",
      label: "Memory Content",
      width: 37,
      render: (value) => (
        <p className="line-clamp-2 whitespace-normal break-words" title={typeof value === "string" ? value : ""}>
          {typeof value === "string" && value !== "" ? value : "No content"}
        </p>
      ),
    },
    {
      key: "categories",
      label: "Categories",
      width: 15,
      render: (_value, row) => (
        <div onClick={(event) => event.stopPropagation()}>
          <CategoriesDisplay categories={row.categories} />
        </div>
      ),
    },
    {
      key: "id",
      label: "Action",
      width: 8,
      align: "right",
      render: (_value, row) => (
        <Button
          type="button"
          size="icon"
          variant="ghost"
          aria-label={`Open memory ${row.id}`}
          onClick={(event) => {
            event.stopPropagation();
            openMemory(row.id);
          }}
        >
          <ChevronRight className="size-4" />
        </Button>
      ),
    },
  // query and URL callbacks intentionally update the action closures.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  ], [query, search]);

  const rows = pageData?.results ?? [];
  const hasInitialError = loadError !== null && pageData === null;

  return (
    <div className="min-w-0 space-y-5">
      <div className="flex min-w-0 flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div className="min-w-0 space-y-1">
          <h1 className="font-fustat text-xl font-semibold">Memories</h1>
          <p className="break-words text-sm text-onSurface-default-secondary">
            Explore, inspect, and update memories stored in this project.
          </p>
        </div>
        <Button type="button" variant="outline" disabled={isLoading || isRefreshing} onClick={refresh}>
          <RefreshCw className={`mr-2 size-4 ${isRefreshing ? "animate-spin" : ""}`} />
          {isRefreshing ? "Refreshing" : "Refresh"}
        </Button>
      </div>

      <div className="flex flex-wrap items-center gap-2">
        <DateRangeFilter value={query.date_range} onChange={changeDateRange} />
        <FilterBuilder
          match={query.match}
          filters={query.filters}
          fields={FILTER_FIELDS}
          onApply={applyCriteria}
          onRemoveAll={(filters) => applyCriteria(query.match, filters)}
        />
      </div>

      {pageData && pageData.stale_skipped > 0 ? (
        <div role="status" className="flex flex-wrap items-center justify-between gap-3 rounded-md border border-amber-300 bg-amber-50 p-3 text-sm text-amber-950">
          <span>{pageData.stale_skipped} stale {pageData.stale_skipped === 1 ? "memory was" : "memories were"} skipped.</span>
          <Button type="button" size="sm" variant="outline" onClick={refresh}>Refresh</Button>
        </div>
      ) : null}

      {loadError !== null ? (
        <div role="alert" className="flex flex-wrap items-center justify-between gap-3 border-y border-memBorder-primary py-3">
          <span className="break-all text-sm text-onSurface-danger-primary">
            {pageData ? `Could not refresh memories: ${loadError}` : `Could not load memories: ${loadError}`}
          </span>
          <Button type="button" size="sm" variant="outline" onClick={refresh}>Retry</Button>
        </div>
      ) : null}

      {isLoading && pageData === null ? (
        <div role="status" className="py-16 text-center text-sm text-onSurface-default-secondary">Loading memories...</div>
      ) : hasInitialError ? null : rows.length === 0 ? (
        <div className="py-16 text-center text-sm text-onSurface-default-secondary">No memories found.</div>
      ) : (
        <>
          <div className="hidden min-w-0 overflow-hidden border-y border-memBorder-primary md:block">
            <DataTable
              data={rows}
              columns={columns}
              getRowKey={(row) => row.id}
              onRowClick={(row) => openMemory(row.id)}
            />
          </div>
          <div className="space-y-2 md:hidden">
            {rows.map((memory) => (
              <button
                key={memory.id}
                type="button"
                className="w-full min-w-0 space-y-3 rounded-md border border-memBorder-primary p-4 text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                onClick={() => openMemory(memory.id)}
              >
                <div className="flex items-start justify-between gap-2">
                  <MemoryTime value={memory.created_at} />
                  <ChevronRight className="size-4 shrink-0" />
                </div>
                <p className="whitespace-normal break-words text-sm">{memory.memory || "No content"}</p>
                <EntityBadges userId={memory.user_id} agentId={memory.agent_id} appId={memory.app_id} runId={memory.run_id} />
                <div onClick={(event) => event.stopPropagation()}>
                  <CategoriesDisplay categories={memory.categories} />
                </div>
              </button>
            ))}
          </div>
        </>
      )}

      {pageData && pageData.total > pageData.page_size ? (
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
              <span className="px-3 text-sm" aria-live="polite">Page {query.page}</span>
            </PaginationItem>
            <PaginationItem>
              <PaginationNext
                href="#"
                isDisabled={!pageData.has_more || isRefreshing}
                aria-disabled={!pageData.has_more || isRefreshing}
                tabIndex={!pageData.has_more || isRefreshing ? -1 : undefined}
                onClick={(event) => {
                  event.preventDefault();
                  writeQuery({ ...query, page: query.page + 1 });
                }}
              />
            </PaginationItem>
          </PaginationContent>
        </Pagination>
      ) : null}

      <MemoryDetailDrawer
        memoryId={memoryId}
        onMemoryIdChange={setDrawerMemoryId}
        onRefreshList={refresh}
        onDeleted={handleDeleted}
      />
    </div>
  );
}

function MemoryTime({ value }: { value: string | null }) {
  if (value === null) {
    return <span className="text-onSurface-default-tertiary">Unknown</span>;
  }
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) {
    return <span title={value}>{value}</span>;
  }
  const difference = date.getTime() - Date.now();
  const absoluteDifference = Math.abs(difference);
  const [amount, unit] = absoluteDifference < 60 * 60 * 1000
    ? [Math.round(difference / (60 * 1000)), "minute" as const]
    : absoluteDifference < 24 * 60 * 60 * 1000
      ? [Math.round(difference / (60 * 60 * 1000)), "hour" as const]
      : [Math.round(difference / (24 * 60 * 60 * 1000)), "day" as const];
  const relative = new Intl.RelativeTimeFormat(undefined, { numeric: "auto" }).format(amount, unit);
  return <time dateTime={value} title={date.toLocaleString()}>{relative}</time>;
}
