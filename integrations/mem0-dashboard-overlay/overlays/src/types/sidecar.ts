export type SidecarCategory = {
  id: string;
  project_id: string;
  name: string;
  description: string;
  schema: Record<string, unknown>;
  enabled: boolean;
  strategy: string;
  version: number;
  created_at: string;
  updated_at: string;
};

export type SidecarCategoryResponse = {
  categories: SidecarCategory[];
};

export type SidecarExportJob = {
  id: string;
  project_id: string;
  status: "PENDING" | "RUNNING" | "SUCCEEDED" | "FAILED";
  format: "json";
  filters: Record<string, string>;
  total_count: number;
  exported_count: number;
  skipped_count: number;
  error: Record<string, unknown>;
  created_at: string;
  started_at: string | null;
  completed_at: string | null;
};

export type SidecarExportListResponse = {
  results: SidecarExportJob[];
};

export type SidecarExportDownload = {
  project_id: string;
  format: "json";
  filters: Record<string, string>;
  memories: unknown[];
  skipped: { id: string; reason: string }[];
};
