"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Copy, RefreshCw } from "lucide-react";

import { EntityBadges } from "@/components/self-hosted/explorer/entity-badges";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { ScrollArea } from "@/components/ui/scroll-area";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { toast } from "@/components/ui/use-toast";
import type { SidecarTrace } from "@/types/sidecar";
import { sidecarGet } from "@/utils/sidecar-api";
import {
  beginTraceDetailRequest,
  canApplyTraceDetailRequest,
  nextTraceRequestGeneration,
} from "@/utils/request-trace-state";

type RequestTraceDrawerProps = {
  requestId: string | null;
  onRequestIdChange: (requestId: string | null) => void;
};

export function RequestTraceDrawer({
  requestId,
  onRequestIdChange,
}: RequestTraceDrawerProps) {
  const [detail, setDetail] = useState<SidecarTrace | null>(null);
  const [isLoading, setIsLoading] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [retryVersion, setRetryVersion] = useState(0);
  const [expandedQuery, setExpandedQuery] = useState(false);
  const requestGeneration = useRef(0);
  const activeRequestIdRef = useRef<string | null>(requestId);
  const mountedRef = useRef(true);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      activeRequestIdRef.current = null;
      requestGeneration.current = nextTraceRequestGeneration(
        requestGeneration.current,
      );
    };
  }, []);

  useEffect(() => {
    activeRequestIdRef.current = requestId;
    const operation =
      requestId === null
        ? null
        : beginTraceDetailRequest(requestGeneration.current, requestId);
    requestGeneration.current =
      operation?.generation ??
      nextTraceRequestGeneration(requestGeneration.current);
    const generation = requestGeneration.current;
    setExpandedQuery(false);

    if (requestId === null) {
      setDetail(null);
      setLoadError(null);
      setIsLoading(false);
      return;
    }

    const controller = new AbortController();
    setDetail(null);
    setLoadError(null);
    setIsLoading(true);

    void sidecarGet<SidecarTrace>(
      `/v1/event/${encodeURIComponent(requestId)}`,
      undefined,
      { signal: controller.signal },
    )
      .then((response) => {
        if (canApplyDetail(requestId, generation, controller.signal)) {
          setDetail(response);
        }
      })
      .catch((error: unknown) => {
        if (
          canApplyDetail(requestId, generation, controller.signal) &&
          !isAbortError(error)
        ) {
          setLoadError(error instanceof Error ? error.message : String(error));
        }
      })
      .finally(() => {
        if (canApplyDetail(requestId, generation, controller.signal)) {
          setIsLoading(false);
        }
      });

    function canApplyDetail(
      targetId: string,
      targetGeneration: number,
      signal: AbortSignal,
    ) {
      return (
        !signal.aborted &&
        canApplyTraceDetailRequest(
          { generation: targetGeneration, targetId },
          requestGeneration.current,
          activeRequestIdRef.current,
          mountedRef.current,
        )
      );
    }

    return () => {
      controller.abort();
    };
  }, [requestId, retryVersion]);

  const requestQuery = useMemo(() => {
    const value = detail?.request.query;
    return typeof value === "string" ? value : null;
  }, [detail]);
  const queryNeedsExpansion =
    requestQuery !== null && requestQuery.length > 240;
  const displayedQuery =
    requestQuery === null
      ? null
      : expandedQuery || !queryNeedsExpansion
        ? requestQuery
        : `${requestQuery.slice(0, 240)}...`;
  const previews = detail?.result_previews.slice(0, 20) ?? [];
  const hasRawError = detail !== null && Object.keys(detail.error).length > 0;

  const copy = useCallback(async (label: string, value: string) => {
    const targetId = activeRequestIdRef.current;
    if (targetId === null) {
      return;
    }
    const copyTarget = {
      generation: requestGeneration.current,
      targetId,
    };
    const canApplyCopyTarget = () =>
      canApplyTraceDetailRequest(
        copyTarget,
        requestGeneration.current,
        activeRequestIdRef.current,
        mountedRef.current,
      );
    try {
      await copyText(value);
      if (canApplyCopyTarget()) {
        toast({ title: `${label} copied`, variant: "success" });
      }
    } catch (error) {
      if (canApplyCopyTarget()) {
        toast({
          title: `Failed to copy ${label.toLowerCase()}`,
          description: error instanceof Error ? error.message : String(error),
          variant: "destructive",
        });
      }
    }
  }, []);

  return (
    <Sheet
      open={requestId !== null}
      onOpenChange={(open) => {
        if (!open) onRequestIdChange(null);
      }}
    >
      <SheetContent
        side="right"
        className="flex w-full max-w-full flex-col gap-0 overflow-x-hidden p-0 sm:max-w-2xl"
      >
        <SheetHeader className="border-b border-memBorder-primary px-5 py-4 pr-12 text-left">
          <div className="flex min-w-0 flex-wrap items-center gap-2">
            <SheetTitle>Event</SheetTitle>
            {detail ? (
              <Badge variant="outline">{detail.display_operation}</Badge>
            ) : null}
          </div>
          <SheetDescription className="break-all">
            Inspect the sanitized request payload and retrieved-memory previews.
          </SheetDescription>
        </SheetHeader>

        <Tabs defaultValue="payload" className="flex min-h-0 flex-1 flex-col">
          <TabsList
            aria-label="Request detail sections"
            className="mx-5 mt-4 grid grid-cols-2"
          >
            <TabsTrigger value="payload">Request Payload</TabsTrigger>
            <TabsTrigger value="memories">Retrieved Memories</TabsTrigger>
          </TabsList>

          <ScrollArea className="min-h-0 flex-1">
            <div className="px-5 py-5">
              {isLoading ? (
                <p
                  role="status"
                  className="text-sm text-onSurface-default-secondary"
                >
                  Loading request details...
                </p>
              ) : null}
              {loadError !== null ? (
                <div role="alert" className="space-y-3">
                  <p className="break-all text-sm text-onSurface-danger-primary">
                    Could not load request details: {loadError}
                  </p>
                  <Button
                    type="button"
                    size="sm"
                    variant="outline"
                    onClick={() => setRetryVersion((value) => value + 1)}
                  >
                    <RefreshCw className="mr-2 size-4" />
                    Retry details
                  </Button>
                </div>
              ) : null}
            </div>

            <TabsContent value="payload" className="m-0 space-y-5 px-5 pb-6">
              {detail ? (
                <>
                  <div className="flex min-w-0 flex-wrap gap-2">
                    <Button
                      type="button"
                      size="sm"
                      variant="outline"
                      onClick={() => void copy("Request ID", detail.id)}
                    >
                      <Copy className="mr-2 size-4" />
                      Copy ID
                    </Button>
                    <Button
                      type="button"
                      size="sm"
                      variant="outline"
                      onClick={() =>
                        void copy("Request JSON", formatJson(detail))
                      }
                    >
                      <Copy className="mr-2 size-4" />
                      Copy JSON
                    </Button>
                  </div>

                  <dl className="grid min-w-0 gap-3 rounded-md border border-memBorder-primary p-4 text-sm sm:grid-cols-2">
                    <TraceField label="ID" value={detail.id} />
                    <TraceField
                      label="Correlation ID"
                      value={detail.correlation_id ?? "--"}
                    />
                    <TraceField label="Status" value={detail.status} />
                    <TraceField
                      label="Latency"
                      value={formatLatency(detail.latency_ms)}
                    />
                    <TraceField
                      label="Requested at"
                      value={formatTimestamp(detail.requested_at)}
                    />
                    <TraceField
                      label="Completed at"
                      value={formatTimestamp(detail.completed_at)}
                    />
                  </dl>

                  <section
                    aria-labelledby="request-entities-heading"
                    className="min-w-0 space-y-2"
                  >
                    <h3 id="request-entities-heading" className="font-semibold">
                      Entities
                    </h3>
                    <EntityBadges
                      userId={detailEntityId(detail, "user")}
                      agentId={detailEntityId(detail, "agent")}
                      appId={detailEntityId(detail, "app")}
                      runId={detailEntityId(detail, "run")}
                    />
                  </section>

                  {displayedQuery !== null ? (
                    <section
                      aria-labelledby="request-search-query"
                      className="space-y-2"
                    >
                      <h3 id="request-search-query" className="font-semibold">
                        Search query
                      </h3>
                      <p className="whitespace-pre-wrap break-words text-sm">
                        {displayedQuery}
                      </p>
                      {queryNeedsExpansion ? (
                        <Button
                          type="button"
                          size="sm"
                          variant="ghost"
                          aria-expanded={expandedQuery}
                          onClick={() => setExpandedQuery((value) => !value)}
                        >
                          {expandedQuery ? "Show less" : "Show more"}
                        </Button>
                      ) : null}
                    </section>
                  ) : null}

                  <JsonSection
                    title="Sanitized request"
                    value={detail.request}
                  />
                  <JsonSection
                    title="Sanitized response"
                    value={detail.response}
                  />
                  {hasRawError ? (
                    <JsonSection
                      title="Raw error"
                      value={detail.error}
                      danger
                    />
                  ) : detail.status === "FAILED" ? (
                    <section
                      aria-labelledby="raw-error-heading"
                      className="space-y-2"
                    >
                      <h3 id="raw-error-heading" className="font-semibold">
                        Raw error
                      </h3>
                      <p className="text-sm text-onSurface-default-secondary">
                        No error payload was recorded.
                      </p>
                    </section>
                  ) : null}
                </>
              ) : null}
            </TabsContent>

            <TabsContent value="memories" className="m-0 space-y-5 px-5 pb-6">
              {detail ? (
                <>
                  <div className="flex min-w-0 flex-wrap items-center justify-between gap-2">
                    <h3 className="font-semibold">Retrieved memories</h3>
                    <span className="text-sm text-onSurface-default-secondary">
                      Result count: {detail.result_count}
                    </span>
                  </div>

                  {detail.result_count === 0 || previews.length === 0 ? (
                    <div className="rounded-md border border-memBorder-primary p-8 text-center">
                      <p className="font-medium">No memories retrieved</p>
                      <p className="mt-1 text-sm text-onSurface-default-secondary">
                        This request returned no stored-memory previews.
                      </p>
                    </div>
                  ) : (
                    <div className="space-y-3">
                      {previews.map((preview, index) => {
                        const previewId = memoryPreviewId(preview);
                        return (
                          <article
                            key={`${previewId ?? "preview"}-${index}`}
                            className="min-w-0 space-y-3 rounded-md border border-memBorder-primary p-4"
                          >
                            <div className="flex min-w-0 flex-wrap items-center justify-between gap-2">
                              <h4 className="break-all font-mono text-xs">
                                {previewId ?? `Memory preview ${index + 1}`}
                              </h4>
                              {previewId ? (
                                <Button
                                  type="button"
                                  size="sm"
                                  variant="ghost"
                                  onClick={() =>
                                    void copy("Memory ID", previewId)
                                  }
                                >
                                  <Copy className="mr-2 size-3.5" />
                                  Copy ID
                                </Button>
                              ) : null}
                            </div>
                            {typeof preview.memory === "string" ? (
                              <p className="whitespace-pre-wrap break-words text-sm">
                                {preview.memory}
                              </p>
                            ) : null}
                            <pre className="max-w-full overflow-x-auto whitespace-pre-wrap break-words rounded bg-surface-default-fg-secondary p-3 text-xs">
                              {formatJson(preview)}
                            </pre>
                          </article>
                        );
                      })}
                    </div>
                  )}

                  {detail.result_previews_omitted > 0 ? (
                    <p
                      role="status"
                      className="text-sm text-onSurface-default-secondary"
                    >
                      {detail.result_previews_omitted} additional result
                      previews were omitted by the server.
                    </p>
                  ) : null}
                  {detail.result_previews_scan_truncated ? (
                    <p
                      role="status"
                      className="text-sm text-onSurface-default-secondary"
                    >
                      Preview collection stopped at the server scan boundary;
                      the result count may exceed the displayed preview set.
                    </p>
                  ) : null}
                  {detail.result_previews.length > 20 ? (
                    <p
                      role="status"
                      className="text-sm text-onSurface-default-secondary"
                    >
                      Only the first 20 sanitized previews are displayed.
                    </p>
                  ) : null}
                </>
              ) : null}
            </TabsContent>
          </ScrollArea>
        </Tabs>
      </SheetContent>
    </Sheet>
  );
}

function TraceField({ label, value }: { label: string; value: string }) {
  return (
    <div className="min-w-0 space-y-1">
      <dt className="text-xs font-semibold text-onSurface-default-secondary">
        {label}
      </dt>
      <dd className="break-all font-mono text-xs">{value}</dd>
    </div>
  );
}

function detailEntityId(
  detail: SidecarTrace,
  type: SidecarTrace["entities"][number]["type"],
): string | null {
  return detail.entities.find((entity) => entity.type === type)?.id ?? null;
}

function JsonSection({
  title,
  value,
  danger = false,
}: {
  title: string;
  value: unknown;
  danger?: boolean;
}) {
  const id = `${title.toLowerCase().replaceAll(" ", "-")}-heading`;
  return (
    <section aria-labelledby={id} className="min-w-0 space-y-2">
      <h3
        id={id}
        className={
          danger
            ? "font-semibold text-onSurface-danger-primary"
            : "font-semibold"
        }
      >
        {title}
      </h3>
      <pre className="max-w-full overflow-x-auto whitespace-pre-wrap break-words rounded-md border border-memBorder-primary bg-surface-default-fg-secondary p-3 text-xs">
        {formatJson(value)}
      </pre>
    </section>
  );
}

async function copyText(value: string): Promise<void> {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(value);
      return;
    }
  } catch {
    // The legacy selection fallback below also works when Clipboard API access is denied.
  }
  const textarea = document.createElement("textarea");
  textarea.value = value;
  textarea.setAttribute("readonly", "");
  textarea.style.position = "fixed";
  textarea.style.opacity = "0";
  document.body.appendChild(textarea);
  textarea.select();
  const copied = document.execCommand("copy");
  textarea.remove();
  if (!copied) {
    throw new Error("Clipboard access is unavailable.");
  }
}

function formatJson(value: unknown): string {
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return '{\n  "_trace_invalid_json": true\n}';
  }
}

function memoryPreviewId(preview: Record<string, unknown>): string | null {
  if (typeof preview.id === "string") return preview.id;
  if (typeof preview.memory_id === "string") return preview.memory_id;
  return null;
}

function isAbortError(error: unknown): boolean {
  return (
    error !== null &&
    typeof error === "object" &&
    "name" in error &&
    error.name === "AbortError"
  );
}

function formatLatency(value: number | null): string {
  return value === null || !Number.isFinite(value)
    ? "--"
    : `${value.toFixed(2)} ms`;
}

function formatTimestamp(value: string | null): string {
  if (value === null) return "--";
  const date = new Date(value);
  return Number.isFinite(date.getTime())
    ? date.toISOString().replace("T", " ").replace(".000Z", " UTC")
    : value;
}
