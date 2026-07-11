"use client";

import { useCallback, useEffect, useState } from "react";
import { Download, FolderInput, RefreshCw } from "lucide-react";
import { formatDistanceToNow } from "date-fns";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { RadioGroup, RadioGroupItem } from "@/components/ui/radio-group";
import { toast } from "@/components/ui/use-toast";
import {
  SidecarExportDownload,
  SidecarExportFilters,
  SidecarExportJob,
  SidecarExportListResponse,
} from "@/types/sidecar";
import { sidecarGet, sidecarPost } from "@/utils/sidecar-api";
import { getSidecarProjectId } from "@/utils/sidecar-project";

function downloadJson(filename: string, payload: SidecarExportDownload) {
  const blob = new Blob([JSON.stringify(payload, null, 2)], {
    type: "application/json",
  });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  window.setTimeout(() => {
    document.body.removeChild(anchor);
    URL.revokeObjectURL(url);
  }, 0);
}

function formatFilterSummary(filters: Record<string, string>): string[] {
  return Object.entries(filters)
    .filter(([, value]) => Boolean(value))
    .map(([key, value]) => `${key}: ${value}`);
}

function formatError(error: Record<string, unknown>): string | null {
  for (const key of ["message", "detail", "reason"]) {
    if (typeof error[key] === "string") return error[key];
  }
  return Object.keys(error).length > 0 ? JSON.stringify(error) : null;
}

function formatTime(value: string | null): string | null {
  if (!value) return null;

  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;

  const absoluteTime = new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(date);
  return `${formatDistanceToNow(new Date(value), { addSuffix: true })} (${absoluteTime})`;
}

export default function ExportPage() {
  const [jobs, setJobs] = useState<SidecarExportJob[]>([]);
  const [projectId, setProjectId] = useState<string | null>(null);
  const [appId, setAppId] = useState("");
  const [userId, setUserId] = useState("");
  const [agentId, setAgentId] = useState("");
  const [runId, setRunId] = useState("");
  const [isLoading, setIsLoading] = useState(true);
  const [isCreating, setIsCreating] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);

  const loadJobs = useCallback(async () => {
    setIsLoading(true);
    setLoadError(null);
    try {
      const resolvedProjectId = await getSidecarProjectId();
      setProjectId(resolvedProjectId);
      const response = await sidecarGet<SidecarExportListResponse>("/v1/exports", {
        project_id: resolvedProjectId,
      });
      setJobs(response.results);
    } catch (error) {
      setProjectId(null);
      setJobs([]);
      setLoadError(error instanceof Error ? error.message : String(error));
      toast({
        title: "Failed to load exports",
        description: error instanceof Error ? error.message : String(error),
        variant: "destructive",
      });
    } finally {
      setIsLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadJobs();
  }, [loadJobs]);

  const createExport = async () => {
    if (!projectId) {
      toast({
        title: "Failed to create export",
        description: "Sidecar project is not loaded.",
        variant: "destructive",
      });
      return;
    }

    setIsCreating(true);
    try {
      const filters = Object.fromEntries(
        Object.entries({
          app_id: appId.trim(),
          user_id: userId.trim(),
          agent_id: agentId.trim(),
          run_id: runId.trim(),
        }).filter(([, value]) => Boolean(value)),
      ) as SidecarExportFilters;
      const created = await sidecarPost<SidecarExportJob>("/v1/exports", {
        project_id: projectId,
        format: "json",
        filters,
      });
      setJobs((current) => [created, ...current.filter((job) => job.id !== created.id)]);
      toast({ title: "Export created", variant: "success" });
    } catch (error) {
      toast({
        title: "Failed to create export",
        description: error instanceof Error ? error.message : String(error),
        variant: "destructive",
      });
    } finally {
      setIsCreating(false);
    }
  };

  const downloadExport = async (job: SidecarExportJob) => {
    if (!projectId) {
      toast({
        title: "Failed to download export",
        description: "Sidecar project is not loaded.",
        variant: "destructive",
      });
      return;
    }

    try {
      const payload = await sidecarGet<SidecarExportDownload>(
        `/v1/exports/${job.id}/download`,
        { project_id: projectId },
      );
      downloadJson(`mem0-export-${job.id}.json`, payload);
    } catch (error) {
      toast({
        title: "Failed to download export",
        description: error instanceof Error ? error.message : String(error),
        variant: "destructive",
      });
    }
  };

  return (
    <div className="space-y-6">
      <div className="flex items-start justify-between gap-4">
        <div className="space-y-1">
          <h1 className="font-fustat text-xl font-semibold">Export</h1>
          <p className="text-sm text-onSurface-default-secondary">
            Export scoped memories from project {projectId ?? "..."}.
          </p>
        </div>
        <Button
          variant="outline"
          onClick={() => void loadJobs()}
          disabled={isLoading}
        >
          <RefreshCw className="mr-2 size-4" />
          Refresh
        </Button>
      </div>

      <Card className="border-memBorder-primary">
        <CardContent className="space-y-5 p-5">
          <div className="flex flex-wrap items-start justify-between gap-3 border-b border-memBorder-primary pb-4">
            <div className="space-y-1">
              <h2 className="font-medium">Create export</h2>
              <p className="text-sm text-onSurface-default-secondary">
                Export memories from this configured project.
              </p>
            </div>
            <div className="min-w-44 space-y-1">
              <Label>Project</Label>
              <p className="truncate font-mono text-sm" title={projectId ?? undefined}>
                {projectId ?? "Loading project..."}
              </p>
            </div>
          </div>

          <fieldset className="space-y-3">
            <legend className="text-sm font-medium">Export format</legend>
            <RadioGroup
              defaultValue="json"
              className="grid gap-2 sm:grid-cols-3"
              aria-label="Export format"
            >
              <Label
                htmlFor="export-format-json"
                className="flex min-h-16 cursor-pointer items-center gap-3 rounded-md border border-memBorder-primary px-3 py-2"
              >
                <RadioGroupItem value="json" id="export-format-json" />
                <span className="space-y-0.5">
                  <span className="block font-medium">JSON</span>
                  <span className="block text-xs font-normal text-onSurface-default-secondary">
                    Structured memory export
                  </span>
                </span>
              </Label>
              <Label
                htmlFor="export-format-csv"
                className="flex min-h-16 cursor-not-allowed items-center gap-3 rounded-md border border-memBorder-primary px-3 py-2 opacity-60"
              >
                <RadioGroupItem value="csv" id="export-format-csv" disabled />
                <span className="space-y-0.5">
                  <span className="block font-medium">CSV</span>
                  <span className="block text-xs font-normal text-onSurface-default-secondary">
                    Coming soon
                  </span>
                </span>
              </Label>
              <Label
                htmlFor="export-format-pydantic"
                className="flex min-h-16 cursor-not-allowed items-center gap-3 rounded-md border border-memBorder-primary px-3 py-2 opacity-60"
              >
                <RadioGroupItem value="pydantic" id="export-format-pydantic" disabled />
                <span className="space-y-0.5">
                  <span className="block font-medium">Pydantic</span>
                  <span className="block text-xs font-normal text-onSurface-default-secondary">
                    Coming soon
                  </span>
                </span>
              </Label>
            </RadioGroup>
          </fieldset>

          <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
            <div className="space-y-2">
              <Label htmlFor="export-app-id">App ID</Label>
              <Input id="export-app-id" value={appId} onChange={(event) => setAppId(event.target.value)} />
            </div>
            <div className="space-y-2">
              <Label htmlFor="export-user-id">User ID</Label>
              <Input id="export-user-id" value={userId} onChange={(event) => setUserId(event.target.value)} />
            </div>
            <div className="space-y-2">
              <Label htmlFor="export-agent-id">Agent ID</Label>
              <Input id="export-agent-id" value={agentId} onChange={(event) => setAgentId(event.target.value)} />
            </div>
            <div className="space-y-2">
              <Label htmlFor="export-run-id">Run ID</Label>
              <Input id="export-run-id" value={runId} onChange={(event) => setRunId(event.target.value)} />
            </div>
          </div>

          <Button onClick={createExport} disabled={isCreating || !projectId}>
            <FolderInput className="mr-2 size-4" />
            Create export
          </Button>
        </CardContent>
      </Card>

      <section className="space-y-3" aria-labelledby="recent-export-jobs">
        <div>
          <h2 id="recent-export-jobs" className="font-medium">Recent jobs</h2>
        </div>
        {isLoading ? (
          <Card className="border-memBorder-primary">
            <CardContent className="p-5">
              <p className="text-sm text-onSurface-default-secondary">
                Loading export jobs...
              </p>
            </CardContent>
          </Card>
        ) : loadError ? (
          <Card className="border-memBorder-primary">
            <CardContent className="flex flex-col items-start gap-3 p-5">
              <div className="space-y-1">
                <p className="text-sm text-onSurface-default-secondary">
                  Failed to load export jobs.
                </p>
                <p className="text-xs text-onSurface-default-tertiary">{loadError}</p>
              </div>
              <Button
                variant="outline"
                onClick={() => void loadJobs()}
                disabled={isLoading}
              >
                Retry load
              </Button>
            </CardContent>
          </Card>
        ) : jobs.length === 0 ? (
          <Card className="border-memBorder-primary">
            <CardContent className="p-5">
              <p className="text-sm text-onSurface-default-secondary">
                No exports yet.
              </p>
            </CardContent>
          </Card>
        ) : (
          jobs.map((job) => (
            <ExportJobRow key={job.id} job={job} onDownload={downloadExport} />
          ))
        )}
      </section>
    </div>
  );
}

function ExportJobRow({
  job,
  onDownload,
}: {
  job: SidecarExportJob;
  onDownload: (job: SidecarExportJob) => Promise<void>;
}) {
  const filterSummary = formatFilterSummary(job.filters);
  const errorSummary = formatError(job.error);

  return (
    <Card className="border-memBorder-primary">
      <CardContent className="grid gap-4 p-4 lg:grid-cols-[minmax(0,1fr)_auto]">
        <div className="min-w-0 space-y-3">
          <div className="flex flex-wrap items-center gap-2">
            <span className="truncate font-mono text-xs" title={job.id}>{job.id}</span>
            <Badge variant="outline">{job.status}</Badge>
            <Badge variant="outline">JSON</Badge>
          </div>
          <div className="flex flex-wrap gap-2">
            {filterSummary.length > 0 ? (
              filterSummary.map((filter) => <Badge key={filter} variant="outline">{filter}</Badge>)
            ) : (
              <span className="text-sm text-onSurface-default-secondary">All scoped memories</span>
            )}
          </div>
          <dl className="grid gap-x-5 gap-y-2 text-sm sm:grid-cols-2 xl:grid-cols-4">
            <div>
              <dt className="text-xs text-onSurface-default-tertiary">Exported</dt>
              <dd>{job.exported_count}</dd>
            </div>
            <div>
              <dt className="text-xs text-onSurface-default-tertiary">Skipped</dt>
              <dd>{job.skipped_count}</dd>
            </div>
            <div>
              <dt className="text-xs text-onSurface-default-tertiary">Created</dt>
              <dd>{formatTime(job.created_at)}</dd>
            </div>
            {job.completed_at ? (
              <div>
                <dt className="text-xs text-onSurface-default-tertiary">Completed</dt>
                <dd>{formatTime(job.completed_at)}</dd>
              </div>
            ) : null}
          </dl>
          {job.status === "FAILED" && errorSummary ? (
            <p className="text-sm text-onSurface-danger-primary">{errorSummary}</p>
          ) : null}
        </div>
        <Button
          variant="outline"
          disabled={job.status !== "SUCCEEDED"}
          onClick={() => void onDownload(job)}
        >
          <Download className="mr-2 size-4" />
          Download
        </Button>
      </CardContent>
    </Card>
  );
}
