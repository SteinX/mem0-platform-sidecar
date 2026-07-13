export type ExplorerMatch = "all" | "any";

export type ExplorerField =
  | "entity_type"
  | "user_id"
  | "agent_id"
  | "app_id"
  | "run_id"
  | "memory_id"
  | "category"
  | "metadata";

export type ExplorerOperator =
  | "equals"
  | "not_equals"
  | "in"
  | "contains";

export type ExplorerFilter = {
  id: string;
  field: ExplorerField;
  operator: ExplorerOperator;
  value: string | string[] | { key: string; value: string };
};

export type ExplorerDateRange = {
  from: string | null;
  to: string | null;
};

export type ExplorerQueryPayload = {
  match: ExplorerMatch;
  filters: ExplorerFilter[];
  date_range: ExplorerDateRange;
  page: number;
  page_size: number;
  sort: "created_at_desc" | "created_at_asc";
};
