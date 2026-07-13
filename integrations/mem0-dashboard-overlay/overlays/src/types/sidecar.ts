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

export type SidecarExportFilterKey =
  | "app_id"
  | "user_id"
  | "agent_id"
  | "run_id";
export type SidecarExportFilters = Partial<
  Record<SidecarExportFilterKey, string>
>;

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

export type SidecarMemoryUpdateResponse = {
  memory: SidecarMemory;
  event: Record<string, unknown>;
};

export type SidecarMemoryHistoryEntry = {
  id?: string;
  memory_id?: string;
  input?: unknown;
  old_memory?: unknown;
  new_memory?: unknown;
  user_id?: string;
  categories?: string[];
  event?: unknown;
  created_at?: unknown;
  updated_at?: unknown;
};

export type SidecarMemoryHistoryResponse = {
  results: unknown;
};

export type SidecarEntity = {
  id: string;
  type: "user" | "agent" | "app" | "run";
  entity_id: string;
  display_name: string | null;
  memory_count: number;
  last_seen_at: string | null;
  updated_at: string | null;
};

export type SidecarEntityQuery = {
  entity_type: "user" | "agent" | "app" | "run";
  match: ExplorerMatch;
  filters: Array<Omit<ExplorerFilter, "id">>;
  date_range: ExplorerDateRange;
  page: number;
  page_size: number;
};

export type SidecarEntityPage = {
  results: SidecarEntity[];
  page: number;
  page_size: number;
  total: number;
  has_more: boolean;
};

export type SidecarEntityDeleteResult = {
  status: "SUCCEEDED" | "PARTIAL" | "FAILED";
  requested_count: number;
  deleted_count: number;
  failed_count: number;
  failed: Array<{ id: string; error: Record<string, unknown> }>;
  event_id: string;
};

export type SidecarTraceTimelineBucket = {
  timestamp: string;
  count: number;
};

export type SidecarTrace = {
  id: string;
  correlation_id: string | null;
  operation: string;
  display_operation:
    | "ADD"
    | "SEARCH"
    | "GET ALL"
    | "UPDATE"
    | "DELETE"
    | "OTHER";
  status: "PENDING" | "RUNNING" | "SUCCEEDED" | "FAILED" | "CANCELLED";
  entities: Array<{ type: "user" | "agent" | "app" | "run"; id: string }>;
  request: Record<string, unknown>;
  response: Record<string, unknown>;
  error: Record<string, unknown>;
  result_count: number;
  has_results: boolean;
  latency_ms: number | null;
  requested_at: string | null;
  completed_at: string | null;
  result_previews: Array<Record<string, unknown>>;
  result_previews_omitted: number;
  result_previews_scan_truncated: boolean;
};

export type SidecarTraceQuery = {
  operation: "ADD" | "SEARCH" | "GET_ALL" | null;
  statuses: Array<"PENDING" | "RUNNING" | "SUCCEEDED" | "FAILED" | "CANCELLED">;
  has_results: boolean | null;
  date_range: ExplorerDateRange;
  entity_filters: Partial<
    Record<"user_id" | "agent_id" | "app_id" | "run_id", string>
  >;
  page: number;
  page_size: number;
};

export type SidecarTracePage = {
  results: SidecarTrace[];
  total: number;
  page: number;
  page_size: number;
  has_more: boolean;
  timeline: SidecarTraceTimelineBucket[];
};
