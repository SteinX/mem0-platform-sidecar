import type {
  ExplorerDateRange,
  ExplorerFilter,
  ExplorerMatch,
} from "@/types/dashboard-explorer";

export type SidecarCategoryInput = {
  name: string;
  description: string;
  schema: Record<string, unknown>;
  enabled: boolean;
  strategy: string;
};

export type SidecarCategoryPatch = Partial<SidecarCategoryInput>;

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

export type SidecarExportFilterKey = "app_id" | "user_id" | "agent_id" | "run_id";
export type SidecarExportFilters = Partial<Record<SidecarExportFilterKey, string>>;

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

export type SidecarMemory = {
  id: string;
  memory: string | null;
  user_id: string | null;
  agent_id: string | null;
  app_id: string | null;
  run_id: string | null;
  categories: string[];
  metadata: Record<string, unknown>;
  created_at: string | null;
  updated_at: string | null;
  expiration_date: string | null;
};

export type SidecarMemoryQuery = {
  match: ExplorerMatch;
  filters: Array<Omit<ExplorerFilter, "id">>;
  date_range: ExplorerDateRange;
  page: number;
  page_size: number;
  sort: "created_at_desc" | "created_at_asc";
};

export type SidecarMemoryPage = {
  results: SidecarMemory[];
  page: number;
  page_size: number;
  total: number;
  has_more: boolean;
  stale_skipped: number;
};

export type SidecarMemoryHistoryEntry = {
  id?: string;
  memory_id?: string;
  input?: { role: string; content: string }[];
  old_memory?: string | null;
  new_memory?: string | null;
  user_id?: string;
  categories?: string[];
  event?: string;
  created_at?: string;
  updated_at?: string;
};
