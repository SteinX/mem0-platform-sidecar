"use client";

import { useEffect, useState } from "react";
import { Download, FolderInput, RefreshCw } from "lucide-react";
import { formatDistanceToNow } from "date-fns";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { toast } from "@/components/ui/use-toast";
import {
  SidecarExportDownload,
  SidecarExportJob,
  SidecarExportListResponse,
} from "@/types/sidecar";
import { sidecarGet, sidecarPost } from "@/utils/sidecar-api";

const PROJECT_ID = "default";

function downloadJson(filename: string, payload: SidecarExportDownload) {
  const blob = new Blob([JSON.stringify(payload, null, 2)], {
    type: "application/json",
  });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  anchor.click();
  URL.revokeObjectURL(url);
}

export default function ExportPage() {
  const [jobs, setJobs] = useState<SidecarExportJob[]>([]);
  const [appId, setAppId] = useState("");
  const [userId, setUserId] = useState("");
  const [agentId, setAgentId] = useState("");
  const [runId, setRunId] = useState("");
  const [isLoading, setIsLoading] = useState(true);
  const [isCreating, setIsCreating] = useState(false);

  const loadJobs = async () => {
    setIsLoading(true);
    try {
      const response = await sidecarGet<SidecarExportListResponse>("/v1/exports", {
        project_id: PROJECT_ID,
      });
      setJobs(response.results);
    } catch (error) {
      toast({
        title: "Failed to load exports",
        description: error instanceof Error ? error.message : String(error),
        variant: "destructive",
      });
    } finally {
      setIsLoading(false);
    }
  };

  useEffect(() => {
    void loadJobs();
  }, []);

  const createExport = async () => {
    setIsCreating(true);
    try {
      const filters = Object.fromEntries(
        Object.entries({
          app_id: appId.trim(),
          user_id: userId.trim(),
          agent_id: agentId.trim(),
          run_id: runId.trim(),
        }).filter(([, value]) => value),
      );
      await sidecarPost<SidecarExportJob>("/v1/exports", {
        project_id: PROJECT_ID,
        format: "json",
        filters,
      });
      toast({ title: "Export created", variant: "success" });
      await loadJobs();
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
    try {
      const payload = await sidecarGet<SidecarExportDownload>(
        `/v1/exports/${job.id}/download`,
        { project_id: PROJECT_ID },
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
            Export scoped memories from project {PROJECT_ID}.
          </p>
        </div>
        <Button variant="outline" onClick={loadJobs} disabled={isLoading}>
          <RefreshCw className="mr-2 size-4" />
          Refresh
        </Button>
      </div>

      <Card className="border-memBorder-primary">
        <CardContent className="grid gap-4 p-5 md:grid-cols-4">
          <div className="space-y-2">
            <Label>App ID</Label>
            <Input value={appId} onChange={(event) => setAppId(event.target.value)} />
          </div>
          <div className="space-y-2">
            <Label>User ID</Label>
            <Input value={userId} onChange={(event) => setUserId(event.target.value)} />
          </div>
          <div className="space-y-2">
            <Label>Agent ID</Label>
            <Input value={agentId} onChange={(event) => setAgentId(event.target.value)} />
          </div>
          <div className="space-y-2">
            <Label>Run ID</Label>
            <Input value={runId} onChange={(event) => setRunId(event.target.value)} />
          </div>
          <div className="md:col-span-4">
            <Button onClick={createExport} disabled={isCreating}>
              <FolderInput className="mr-2 size-4" />
              Create JSON Export
            </Button>
          </div>
        </CardContent>
      </Card>

      <div className="grid gap-3">
        {jobs.map((job) => (
          <Card key={job.id} className="border-memBorder-primary">
            <CardContent className="flex flex-wrap items-center justify-between gap-4 p-4">
              <div className="space-y-1">
                <div className="flex items-center gap-2">
                  <span className="font-mono text-xs">{job.id}</span>
                  <Badge variant="outline">{job.status}</Badge>
                </div>
                <p className="text-sm text-onSurface-default-secondary">
                  {job.exported_count} exported, {job.skipped_count} skipped
                </p>
                <p className="text-xs text-onSurface-default-tertiary">
                  Created {formatDistanceToNow(new Date(job.created_at), { addSuffix: true })}
                </p>
              </div>
              <Button
                variant="outline"
                disabled={job.status !== "SUCCEEDED"}
                onClick={() => void downloadExport(job)}
              >
                <Download className="mr-2 size-4" />
                Download
              </Button>
            </CardContent>
          </Card>
        ))}
      </div>
    </div>
  );
}
