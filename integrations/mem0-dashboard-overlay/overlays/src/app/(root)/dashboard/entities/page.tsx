"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ChevronRight, RefreshCw, Trash2 } from "lucide-react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";

import { DataTable } from "@/components/shared/data-table";
import { DateRangeFilter } from "@/components/self-hosted/explorer/date-range-filter";
import { EntityBadges } from "@/components/self-hosted/explorer/entity-badges";
import { sanitizeExplorerError as sanitizeDisplayedError } from "@/components/self-hosted/explorer/explorer-component-state";
import {
  FilterBuilder,
  type ExplorerFilterFieldOption,
} from "@/components/self-hosted/explorer/filter-builder";
import {
  AlertDialog,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Pagination,
  PaginationContent,
  PaginationItem,
  PaginationNext,
  PaginationPrevious,
} from "@/components/ui/pagination";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import type {
  ExplorerDateRange,
  ExplorerFilter,
  ExplorerMatch,
  ExplorerQueryPayload,
} from "@/types/dashboard-explorer";
import type {
  SidecarEntity,
  SidecarEntityDeleteResult,
  SidecarEntityPage,
  SidecarEntityQuery,
} from "@/types/sidecar";
import {
  canApplyExplorerDetailRequest,
  createExplorerFilter,
  normalizeEntityExplorerFilters,
  readExplorerUrlState,
  writeExplorerUrlState,
} from "@/utils/explorer-query-state";
import { sidecarGet, sidecarQuery } from "@/utils/sidecar-api";

type EntityType = SidecarEntity["type"];
type EntityExplorerQuery = Omit<SidecarEntityQuery, "filters"> & {
  filters: ExplorerFilter[];
};

type EntityColumn = {
  key: keyof SidecarEntity;
  label: string;
  width?: number;
  align?: "left" | "center" | "right";
  render?: (
    value: SidecarEntity[keyof SidecarEntity],
    row: SidecarEntity,
  ) => React.ReactNode;
};

const DEFAULT_QUERY: EntityExplorerQuery = {
  entity_type: "user",
  match: "all",
  filters: [],
  date_range: { from: null, to: null },
  page: 1,
  page_size: 20,
};

const ENTITY_TYPE_TABS: Array<{ label: string; value: EntityType }> = [
  { label: "USER", value: "user" },
  { label: "RUN", value: "run" },
  { label: "AGENT", value: "agent" },
  { label: "APP", value: "app" },
];

const FILTER_FIELDS: ExplorerFilterFieldOption[] = [
  {
    value: "entity_type",
    label: "Entity type",
    options: ENTITY_TYPE_TABS.map(({ label, value }) => ({ value, label })),
  },
  { value: "user_id", label: "User ID" },
  { value: "run_id", label: "Run ID" },
  { value: "agent_id", label: "Agent ID" },
  { value: "app_id", label: "App ID" },
];

const ENTITY_MEMORY_FIELDS: Record<
  EntityType,
  "user_id" | "agent_id" | "app_id" | "run_id"
> = {
  user: "user_id",
  agent: "agent_id",
  app: "app_id",
  run: "run_id",
};

const MEMORY_QUERY: ExplorerQueryPayload = {
  match: "all",
  filters: [],
  date_range: { from: null, to: null },
  page: 1,
  page_size: 20,
  sort: "created_at_desc",
};

export default function EntitiesPage() {
  const pathname = usePathname();
  const router = useRouter();
  const searchParams = useSearchParams();
  const search = searchParams.toString();
  const [query, setQuery] = useState<EntityExplorerQuery>(DEFAULT_QUERY);
  const [hydrated, setHydrated] = useState(false);
  const [pageData, setPageData] = useState<SidecarEntityPage | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [isRefreshing, setIsRefreshing] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [refreshVersion, setRefreshVersion] = useState(0);
  const [preparingEntityKey, setPreparingEntityKey] = useState<string | null>(
    null,
  );
  const [deletePreparationError, setDeletePreparationError] = useState<
    string | null
  >(null);
  const [selectedEntity, setSelectedEntity] = useState<SidecarEntity | null>(
    null,
  );
  const [confirmationText, setConfirmationText] = useState("");
  const [isDeleting, setIsDeleting] = useState(false);
  const [deleteResult, setDeleteResult] =
    useState<SidecarEntityDeleteResult | null>(null);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  const [rowsAreAuthoritative, setRowsAreAuthoritative] = useState(false);
  const listGeneration = useRef(0);
  const detailGeneration = useRef(0);
  const queryContextGeneration = useRef(0);
  const deleteGeneration = useRef(0);
  const queryRef = useRef<EntityExplorerQuery>(DEFAULT_QUERY);
  const pageDataRef = useRef<SidecarEntityPage | null>(null);
  const detailControllerRef = useRef<AbortController | null>(null);
  const mountedRef = useRef(true);
  const pageHeadingRef = useRef<HTMLHeadingElement>(null);
  const deleteOpenerRef = useRef<HTMLElement | null>(null);

  const invalidateEntityDetailForQueryTransition = useCallback(() => {
    detailControllerRef.current?.abort();
    detailControllerRef.current = null;
    detailGeneration.current += 1;
    queryContextGeneration.current += 1;
    setPreparingEntityKey(null);
  }, []);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      detailControllerRef.current?.abort();
      listGeneration.current += 1;
      detailGeneration.current += 1;
      queryContextGeneration.current += 1;
      deleteGeneration.current += 1;
      deleteOpenerRef.current = null;
    };
  }, []);

  useEffect(() => {
    const currentParams = new URLSearchParams(search);
    const nextQuery = readEntityUrlState(currentParams);
    const canonicalParams = writeEntityUrlState(currentParams, nextQuery);
    const canonicalSearch = canonicalParams.toString();
    if (canonicalSearch !== search) {
      router.replace(
        canonicalSearch ? `${pathname}?${canonicalSearch}` : pathname,
      );
    }
    if (!entityQueriesEqual(queryRef.current, nextQuery)) {
      invalidateEntityDetailForQueryTransition();
      setRowsAreAuthoritative(false);
      queryRef.current = nextQuery;
      setQuery(nextQuery);
    }
    setHydrated(true);
  }, [invalidateEntityDetailForQueryTransition, pathname, router, search]);

  const replaceParams = useCallback(
    (next: URLSearchParams) => {
      const value = next.toString();
      router.replace(value ? `${pathname}?${value}` : pathname);
    },
    [pathname, router],
  );

  const writeQuery = useCallback(
    (next: EntityExplorerQuery) => {
      if (!entityQueriesEqual(queryRef.current, next)) {
        invalidateEntityDetailForQueryTransition();
        setRowsAreAuthoritative(false);
        queryRef.current = next;
        setQuery(next);
      }
      replaceParams(writeEntityUrlState(new URLSearchParams(search), next));
    },
    [invalidateEntityDetailForQueryTransition, replaceParams, search],
  );

  useEffect(() => {
    if (!hydrated) {
      return;
    }
    const controller = new AbortController();
    const generation = listGeneration.current + 1;
    const contextGeneration = queryContextGeneration.current;
    listGeneration.current = generation;
    if (pageDataRef.current === null) {
      setIsLoading(true);
    } else {
      setIsRefreshing(true);
    }
    setLoadError(null);

    void sidecarQuery<SidecarEntityPage>(
      "/v1/entities/query",
      entityQueryPayload(query),
      { signal: controller.signal },
    )
      .then((response) => {
        if (
          !controller.signal.aborted &&
          mountedRef.current &&
          generation === listGeneration.current &&
          contextGeneration === queryContextGeneration.current
        ) {
          pageDataRef.current = response;
          setPageData(response);
          setRowsAreAuthoritative(true);
        }
      })
      .catch((error: unknown) => {
        if (
          !controller.signal.aborted &&
          mountedRef.current &&
          generation === listGeneration.current &&
          contextGeneration === queryContextGeneration.current &&
          !isAbortError(error)
        ) {
          setLoadError(
            sanitizeDisplayedError(error, "Could not load entities"),
          );
        }
      })
      .finally(() => {
        if (
          !controller.signal.aborted &&
          mountedRef.current &&
          generation === listGeneration.current &&
          contextGeneration === queryContextGeneration.current
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
    (match: ExplorerMatch, filters: ExplorerFilter[]) => {
      writeQuery({ ...query, match, filters, page: 1 });
    },
    [query, writeQuery],
  );

  const changeDateRange = useCallback(
    (dateRange: ExplorerDateRange) => {
      writeQuery({ ...query, date_range: dateRange, page: 1 });
    },
    [query, writeQuery],
  );

  const selectEntityType = useCallback(
    (entityType: EntityType) => {
      writeQuery({ ...query, entity_type: entityType, page: 1 });
    },
    [query, writeQuery],
  );

  const viewMemories = useCallback(
    (entity: SidecarEntity) => {
      const filter = createExplorerFilter({
        field: ENTITY_MEMORY_FIELDS[entity.type],
        operator: "equals",
        value: entity.entity_id,
      });
      const destination = writeExplorerUrlState(new URLSearchParams(), {
        ...MEMORY_QUERY,
        filters: [filter],
      });
      router.push(`/dashboard/memories?${destination.toString()}`);
    },
    [router],
  );

  const openDeleteDialog = useCallback(
    async (entity: SidecarEntity, opener: HTMLElement | null) => {
      if (!rowsAreAuthoritative) {
        return;
      }
      detailControllerRef.current?.abort();
      const controller = new AbortController();
      detailControllerRef.current = controller;
      const generation = detailGeneration.current + 1;
      detailGeneration.current = generation;
      const target = {
        requestGeneration: generation,
        contextGeneration: queryContextGeneration.current,
      };
      const entityKey = `${entity.type}:${entity.id}`;
      setPreparingEntityKey(entityKey);
      setDeletePreparationError(null);
      try {
        const detail = await sidecarGet<SidecarEntity>(
          `/v1/entities/${encodeURIComponent(entity.type)}/${encodeURIComponent(entity.entity_id)}`,
          undefined,
          { signal: controller.signal },
        );
        if (
          !controller.signal.aborted &&
          canApplyExplorerDetailRequest(
            target,
            detailGeneration.current,
            queryContextGeneration.current,
            mountedRef.current,
          )
        ) {
          deleteOpenerRef.current = opener?.isConnected ? opener : null;
          setConfirmationText("");
          setDeleteResult(null);
          setDeleteError(null);
          setSelectedEntity(detail);
        }
      } catch (error) {
        if (
          !controller.signal.aborted &&
          canApplyExplorerDetailRequest(
            target,
            detailGeneration.current,
            queryContextGeneration.current,
            mountedRef.current,
          ) &&
          !isAbortError(error)
        ) {
          setDeletePreparationError(
            sanitizeDisplayedError(error, "Could not open delete confirmation"),
          );
        }
      } finally {
        if (
          !controller.signal.aborted &&
          canApplyExplorerDetailRequest(
            target,
            detailGeneration.current,
            queryContextGeneration.current,
            mountedRef.current,
          )
        ) {
          setPreparingEntityKey(null);
          detailControllerRef.current = null;
        }
      }
    },
    [rowsAreAuthoritative],
  );

  const closeDeleteDialog = useCallback(() => {
    if (isDeleting) {
      return;
    }
    detailGeneration.current += 1;
    deleteGeneration.current += 1;
    setSelectedEntity(null);
    setConfirmationText("");
    setDeleteResult(null);
    setDeleteError(null);
  }, [isDeleting]);

  const restoreDeleteFocus = useCallback(() => {
    if (!mountedRef.current) {
      return;
    }
    const opener = deleteOpenerRef.current;
    deleteOpenerRef.current = null;
    const target = opener?.isConnected ? opener : pageHeadingRef.current;
    if (target?.isConnected) {
      target.focus();
    }
  }, []);

  const isConfirmationExact =
    selectedEntity !== null && confirmationText === selectedEntity.entity_id;

  async function deleteSelectedEntity() {
    if (
      selectedEntity === null ||
      !isConfirmationExact ||
      isDeleting ||
      deleteResult !== null
    ) {
      return;
    }
    const target = selectedEntity;
    const generation = deleteGeneration.current + 1;
    deleteGeneration.current = generation;
    setIsDeleting(true);
    setDeleteError(null);
    try {
      const result = await deleteEntity(target);
      if (!mountedRef.current || generation !== deleteGeneration.current) {
        return;
      }
      if (result.status === "SUCCEEDED") {
        setSelectedEntity(null);
        setConfirmationText("");
        refresh();
        return;
      }
      if (result.status === "PARTIAL") {
        setDeleteResult(result);
        refresh();
        return;
      }
      if (result.status === "FAILED") {
        setDeleteResult(result);
      }
    } catch (error) {
      if (mountedRef.current && generation === deleteGeneration.current) {
        setDeleteError(
          sanitizeDisplayedError(error, "Could not delete entity"),
        );
      }
    } finally {
      if (mountedRef.current && generation === deleteGeneration.current) {
        setIsDeleting(false);
      }
    }
  }

  const columns = useMemo<EntityColumn[]>(
    () => [
      {
        key: "entity_id",
        label: "Entities",
        width: 42,
        render: (_value, entity) => (
          <EntityBadges
            entity={{
              type: entity.type,
              id: entity.entity_id,
              displayName: entity.display_name,
            }}
          />
        ),
      },
      {
        key: "updated_at",
        label: "Updated On",
        width: 22,
        render: (_value, entity) => (
          <EntityTime value={entity.updated_at ?? entity.last_seen_at} />
        ),
      },
      {
        key: "memory_count",
        label: "Memories",
        width: 13,
        render: (value) => (
          <span>
            {typeof value === "number" ? value.toLocaleString() : "0"}
          </span>
        ),
      },
      {
        key: "id",
        label: "Action",
        width: 23,
        align: "right",
        render: (_value, entity) => (
          <EntityActions
            entity={entity}
            canDelete={rowsAreAuthoritative}
            isPreparing={preparingEntityKey === `${entity.type}:${entity.id}`}
            onView={() => viewMemories(entity)}
            onDelete={(opener) => void openDeleteDialog(entity, opener)}
          />
        ),
      },
    ],
    [openDeleteDialog, preparingEntityKey, rowsAreAuthoritative, viewMemories],
  );

  const rows = pageData?.results ?? [];
  const hasInitialError = loadError !== null && pageData === null;

  return (
    <div className="min-w-0 space-y-5">
      <div className="flex min-w-0 flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div className="min-w-0 space-y-1">
          <h1
            ref={pageHeadingRef}
            tabIndex={-1}
            className="font-fustat text-xl font-semibold focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          >
            Entities
          </h1>
          <p className="break-words text-sm text-onSurface-default-secondary">
            Explore scoped identities and the memories associated with them.
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

      <Tabs
        value={query.entity_type}
        onValueChange={(value) => selectEntityType(value as EntityType)}
      >
        <TabsList
          className="h-auto max-w-full flex-wrap justify-start"
          aria-label="Entity type"
        >
          {ENTITY_TYPE_TABS.map((tab) => (
            <TabsTrigger key={tab.value} value={tab.value}>
              {tab.label}
            </TabsTrigger>
          ))}
        </TabsList>
      </Tabs>

      <div className="flex flex-wrap items-center gap-2">
        <DateRangeFilter value={query.date_range} onChange={changeDateRange} />
        <FilterBuilder
          match={query.match}
          filters={query.filters}
          fields={FILTER_FIELDS}
          onApply={applyCriteria}
          onRemoveAll={(filters) => applyCriteria(query.match, filters)}
        />
        <span
          className="text-xs text-onSurface-default-tertiary"
          aria-live="polite"
        >
          {query.filters.length} active{" "}
          {query.filters.length === 1 ? "filter" : "filters"}
        </span>
      </div>

      {deletePreparationError !== null ? (
        <div
          role="alert"
          className="flex flex-wrap items-center justify-between gap-3 border-y border-memBorder-primary py-3"
        >
          <span className="break-words text-sm text-onSurface-danger-primary">
            {deletePreparationError}
          </span>
          <Button
            type="button"
            size="sm"
            variant="outline"
            onClick={() => setDeletePreparationError(null)}
          >
            Dismiss
          </Button>
        </div>
      ) : null}

      {loadError !== null ? (
        <div
          role="alert"
          className="flex flex-wrap items-center justify-between gap-3 border-y border-memBorder-primary py-3"
        >
          <span className="break-words text-sm text-onSurface-danger-primary">
            {pageData
              ? `Could not refresh entities: ${loadError}`
              : `Could not load entities: ${loadError}`}
          </span>
          <Button type="button" size="sm" variant="outline" onClick={refresh}>
            Retry
          </Button>
        </div>
      ) : null}

      {pageData !== null ? (
        <div className="flex items-baseline justify-between gap-3">
          <h2 className="font-semibold">Entity results</h2>
          <span className="text-sm text-onSurface-default-secondary">
            {pageData.total} total
          </span>
        </div>
      ) : null}

      {isLoading && pageData === null ? (
        <div
          role="status"
          className="py-16 text-center text-sm text-onSurface-default-secondary"
        >
          Loading entities...
        </div>
      ) : hasInitialError ? null : rows.length === 0 ? (
        <div className="py-16 text-center text-sm text-onSurface-default-secondary">
          No entities found.
        </div>
      ) : (
        <>
          <div className="hidden min-w-0 overflow-hidden border-y border-memBorder-primary md:block">
            <DataTable
              data={rows}
              columns={columns}
              getRowKey={(entity) => entity.id}
            />
          </div>
          <div className="space-y-2 md:hidden">
            {rows.map((entity) => (
              <article
                key={entity.id}
                className="min-w-0 space-y-4 rounded-md border border-memBorder-primary p-4"
              >
                <EntityBadges
                  entity={{
                    type: entity.type,
                    id: entity.entity_id,
                    displayName: entity.display_name,
                  }}
                />
                <dl className="grid grid-cols-2 gap-3 text-sm">
                  <div>
                    <dt className="text-onSurface-default-secondary">
                      Updated On
                    </dt>
                    <dd>
                      <EntityTime
                        value={entity.updated_at ?? entity.last_seen_at}
                      />
                    </dd>
                  </div>
                  <div>
                    <dt className="text-onSurface-default-secondary">
                      Memories
                    </dt>
                    <dd>{entity.memory_count.toLocaleString()}</dd>
                  </div>
                </dl>
                <EntityActions
                  entity={entity}
                  canDelete={rowsAreAuthoritative}
                  isPreparing={
                    preparingEntityKey === `${entity.type}:${entity.id}`
                  }
                  onView={() => viewMemories(entity)}
                  onDelete={(opener) => void openDeleteDialog(entity, opener)}
                />
              </article>
            ))}
          </div>
        </>
      )}

      {pageData && (query.page > 1 || pageData.has_more) ? (
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

      <AlertDialog
        open={selectedEntity !== null}
        onOpenChange={(open) => {
          if (!open) {
            closeDeleteDialog();
          }
        }}
      >
        <AlertDialogContent
          onCloseAutoFocus={(event) => {
            event.preventDefault();
            restoreDeleteFocus();
          }}
        >
          <AlertDialogHeader>
            <AlertDialogTitle>Delete entity and its memories?</AlertDialogTitle>
            <AlertDialogDescription>
              This permanently deletes every memory currently scoped to this
              entity. The current projected memory count is{" "}
              <strong>{selectedEntity?.memory_count ?? 0}</strong>.
            </AlertDialogDescription>
          </AlertDialogHeader>

          {selectedEntity !== null ? (
            <div className="space-y-3">
              <p className="break-all text-sm font-medium">
                {selectedEntity.entity_id}
              </p>
              <label
                htmlFor="entity-delete-confirmation"
                className="block text-sm text-onSurface-default-secondary"
              >
                Type the exact case-sensitive entity ID to confirm.
              </label>
              <Input
                id="entity-delete-confirmation"
                value={confirmationText}
                onChange={(event) => setConfirmationText(event.target.value)}
                autoComplete="off"
                spellCheck={false}
                aria-describedby="entity-delete-confirmation-help"
              />
              <p
                id="entity-delete-confirmation-help"
                className="text-xs text-onSurface-default-tertiary"
              >
                The value must match exactly, including capitalization.
              </p>
            </div>
          ) : null}

          {deleteResult?.status === "PARTIAL" ? (
            <div
              role="status"
              className="space-y-2 rounded-md border border-amber-300 bg-amber-50 p-3 text-sm text-amber-950"
            >
              <p>
                Partial deletion: {deleteResult.deleted_count} deleted and{" "}
                {deleteResult.failed_count} failed. The entity list was
                refreshed.
              </p>
              <DeleteFailureList result={deleteResult} />
            </div>
          ) : null}

          {deleteResult?.status === "FAILED" ? (
            <div
              role="alert"
              className="space-y-2 rounded-md border border-memBorder-primary p-3 text-sm text-onSurface-danger-primary"
            >
              <p>
                Deletion failed: {deleteResult.deleted_count} deleted and{" "}
                {deleteResult.failed_count} failed.
              </p>
              <DeleteFailureList result={deleteResult} />
            </div>
          ) : null}

          {deleteError !== null ? (
            <p role="alert" className="text-sm text-onSurface-danger-primary">
              {deleteError}
            </p>
          ) : null}

          <AlertDialogFooter>
            <AlertDialogCancel disabled={isDeleting}>
              {deleteResult === null ? "Cancel" : "Close"}
            </AlertDialogCancel>
            <Button
              type="button"
              variant="destructive"
              disabled={
                selectedEntity === null ||
                !isConfirmationExact ||
                isDeleting ||
                deleteResult !== null
              }
              onClick={() => void deleteSelectedEntity()}
            >
              <Trash2 className="mr-2 size-4" />
              {isDeleting ? "Deleting" : "Delete entity"}
            </Button>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}

function EntityActions({
  entity,
  canDelete,
  isPreparing,
  onView,
  onDelete,
}: {
  entity: SidecarEntity;
  canDelete: boolean;
  isPreparing: boolean;
  onView: () => void;
  onDelete: (opener: HTMLElement) => void;
}) {
  return (
    <div className="flex min-w-0 flex-wrap items-center justify-end gap-1">
      <Button
        type="button"
        size="sm"
        variant="ghost"
        aria-label={`View memories for ${entity.entity_id}`}
        onClick={(event) => {
          event.stopPropagation();
          onView();
        }}
      >
        View memories
        <ChevronRight className="ml-1 size-4" />
      </Button>
      <Button
        type="button"
        size="icon"
        variant="ghost"
        disabled={!canDelete || isPreparing}
        aria-label={`Delete ${entity.type} entity ${entity.entity_id}`}
        onClick={(event) => {
          event.stopPropagation();
          onDelete(event.currentTarget);
        }}
      >
        {isPreparing ? (
          <RefreshCw className="size-4 animate-spin" />
        ) : (
          <Trash2 className="size-4 text-onSurface-danger-primary" />
        )}
      </Button>
    </div>
  );
}

function DeleteFailureList({ result }: { result: SidecarEntityDeleteResult }) {
  if (result.failed.length === 0) {
    return null;
  }
  return (
    <ul className="list-disc space-y-1 pl-5">
      {result.failed.map((failure, index) => (
        <li key={`${failure.id}:${index}`} className="break-words">
          <span className="break-all font-mono">{failure.id}</span>
          {": "}
          {sanitizeDisplayedError(
            failure.error,
            "Upstream memory deletion failed",
          )}
        </li>
      ))}
    </ul>
  );
}

function EntityTime({ value }: { value: string | null }) {
  if (value === null) {
    return <span className="text-onSurface-default-tertiary">Unknown</span>;
  }
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) {
    return <span title={value}>{value}</span>;
  }
  return (
    <time dateTime={value} title={date.toLocaleString()}>
      {date.toLocaleDateString()}
    </time>
  );
}

function readEntityUrlState(
  searchParams: URLSearchParams,
): EntityExplorerQuery {
  const shared = readExplorerUrlState(searchParams);
  return {
    entity_type: shared.entityType ?? "user",
    match: shared.match,
    filters: normalizeEntityExplorerFilters(shared.filters),
    date_range: shared.date_range,
    page: shared.page,
    page_size: shared.page_size,
  };
}

function writeEntityUrlState(
  current: URLSearchParams,
  query: EntityExplorerQuery,
): URLSearchParams {
  const next = writeExplorerUrlState(current, {
    match: query.match,
    filters: normalizeEntityExplorerFilters(query.filters),
    date_range: query.date_range,
    page: query.page,
    page_size: query.page_size,
    sort: "created_at_desc",
  });
  next.set("entityType", query.entity_type);
  return next;
}

function entityQueryPayload(query: EntityExplorerQuery): SidecarEntityQuery {
  const filters = normalizeEntityExplorerFilters(query.filters);
  return {
    entity_type: query.entity_type,
    match: query.match,
    filters: filters.map(({ id: _id, ...filter }) => filter),
    date_range: query.date_range,
    page: query.page,
    page_size: query.page_size,
  };
}

function entityQueriesEqual(
  left: EntityExplorerQuery,
  right: EntityExplorerQuery,
): boolean {
  return JSON.stringify(left) === JSON.stringify(right);
}

async function deleteEntity(
  entity: SidecarEntity,
): Promise<SidecarEntityDeleteResult> {
  const response = await fetch(
    `/api/sidecar/v1/entities/${encodeURIComponent(entity.type)}/${encodeURIComponent(entity.entity_id)}`,
    { method: "DELETE" },
  );
  if (!response.ok) {
    throw new Error(`Entity deletion request failed (${response.status})`);
  }
  const result: unknown = await response.json().catch(() => null);
  if (!isEntityDeleteResult(result)) {
    throw new Error("Entity deletion returned an invalid response");
  }
  return result;
}

function isEntityDeleteResult(
  value: unknown,
): value is SidecarEntityDeleteResult {
  if (value === null || typeof value !== "object") {
    return false;
  }
  const result = value as Record<string, unknown>;
  return (
    (result.status === "SUCCEEDED" ||
      result.status === "PARTIAL" ||
      result.status === "FAILED") &&
    typeof result.requested_count === "number" &&
    typeof result.deleted_count === "number" &&
    typeof result.failed_count === "number" &&
    Array.isArray(result.failed) &&
    typeof result.event_id === "string"
  );
}

function isAbortError(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError";
}
