import type { ExplorerQueryPayload } from "@/types/dashboard-explorer";
import type {
  SidecarMemory,
  SidecarMemoryQuery,
} from "@/types/sidecar";

export type MemoryDraft = {
  text: string;
  metadataText: string;
  expiration: string;
};

export type ParsedMemoryHistory = {
  sourceMessages: Array<{ role: string; content: string }>;
  updates: Array<{
    event: string;
    oldMemory: string | null;
    newMemory: string | null;
    timestamp: string | null;
  }>;
};

export function memoryQueryPayload(
  query: ExplorerQueryPayload,
): SidecarMemoryQuery {
  return {
    ...query,
    filters: query.filters.map(({ id: _id, ...filter }) => filter),
  };
}

export function resetMemoryQueryPage(
  query: ExplorerQueryPayload,
): ExplorerQueryPayload {
  return { ...query, page: 1 };
}

export function memoryQueriesEqual(
  left: ExplorerQueryPayload,
  right: ExplorerQueryPayload,
): boolean {
  return canonicalJson(left) === canonicalJson(right);
}

export function nextMemoryRequestGeneration(current: number): number {
  return current + 1;
}

export function isCurrentMemoryRequest(
  requestGeneration: number,
  currentGeneration: number,
): boolean {
  return requestGeneration === currentGeneration;
}

export function initializeMemoryDraft(
  memory: Pick<SidecarMemory, "memory" | "metadata" | "expiration_date">,
): MemoryDraft {
  return {
    text: memory.memory ?? "",
    metadataText: JSON.stringify(memory.metadata ?? {}, null, 2),
    expiration: memory.expiration_date ?? "",
  };
}

export function isMemoryDraftDirty(
  draft: MemoryDraft,
  initial: MemoryDraft,
): boolean {
  if (
    draft.text !== initial.text
    || normalizeExpiration(draft.expiration) !== normalizeExpiration(initial.expiration)
  ) {
    return true;
  }
  try {
    return canonicalJson(parseMemoryMetadataObject(draft.metadataText))
      !== canonicalJson(parseMemoryMetadataObject(initial.metadataText));
  } catch {
    return draft.metadataText !== initial.metadataText;
  }
}

export function isMemoryDraftReady(
  activeMemoryId: string | null,
  draftMemoryId: string | null,
  detailMemoryId: string | null,
): boolean {
  return activeMemoryId !== null
    && activeMemoryId === draftMemoryId
    && activeMemoryId === detailMemoryId;
}

export function parseMemoryMetadataObject(value: string): Record<string, unknown> {
  let parsed: unknown;
  try {
    parsed = JSON.parse(value);
  } catch {
    throw new Error("Metadata must be valid JSON.");
  }
  if (parsed === null || Array.isArray(parsed) || typeof parsed !== "object") {
    throw new Error("Metadata must be a JSON object.");
  }
  return parsed as Record<string, unknown>;
}

export function buildMemoryPatch(
  draft: MemoryDraft,
  initial: MemoryDraft,
): Record<string, unknown> {
  const patch: Record<string, unknown> = {};
  if (draft.text !== initial.text) {
    patch.text = draft.text;
  }
  const metadata = parseMemoryMetadataObject(draft.metadataText);
  const initialMetadata = parseMemoryMetadataObject(initial.metadataText);
  if (canonicalJson(metadata) !== canonicalJson(initialMetadata)) {
    patch.metadata = metadata;
  }
  const expiration = normalizeExpiration(draft.expiration);
  if (expiration !== normalizeExpiration(initial.expiration)) {
    patch.expiration_date = expiration === ""
      ? null
      : expiration;
  }
  return patch;
}

export function parseMemoryHistory(
  entries: unknown,
): ParsedMemoryHistory {
  const sourceMessages: Array<{ role: string; content: string }> = [];
  const updates: ParsedMemoryHistory["updates"] = [];
  if (!Array.isArray(entries)) {
    return { sourceMessages, updates };
  }
  for (const candidate of entries) {
    if (candidate === null || Array.isArray(candidate) || typeof candidate !== "object") {
      continue;
    }
    const entry = candidate as Record<string, unknown>;
    if (Array.isArray(entry.input)) {
      for (const message of entry.input) {
        if (
          message !== null
          && typeof message === "object"
          && typeof message.role === "string"
          && typeof message.content === "string"
        ) {
          sourceMessages.push({ role: message.role, content: message.content });
        }
      }
    }
    updates.push({
      event: typeof entry.event === "string" && entry.event.trim() !== ""
        ? entry.event
        : "Unknown update",
      oldMemory: typeof entry.old_memory === "string" ? entry.old_memory : null,
      newMemory: typeof entry.new_memory === "string" ? entry.new_memory : null,
      timestamp: validTimestamp(entry.updated_at) ?? validTimestamp(entry.created_at),
    });
  }
  return { sourceMessages, updates };
}

export function pageAfterMemoryDelete(
  page: number,
  rowCount: number,
  deletedMemoryIsOnPage = true,
): number {
  return page > 1 && rowCount === 1 && deletedMemoryIsOnPage ? page - 1 : page;
}

export function memoryDeleteNavigation(
  current: URLSearchParams,
  page: number,
  rowCount: number,
  deletedMemoryIsOnPage = true,
): { page: number; searchParams: URLSearchParams } {
  return {
    page: pageAfterMemoryDelete(page, rowCount, deletedMemoryIsOnPage),
    searchParams: closeMemoryUrl(current),
  };
}

export function memoryApiPath(memoryId: string): string {
  return `/v1/memories/${encodeURIComponent(memoryId)}`;
}

export function setMemoryIdInUrl(
  current: URLSearchParams,
  memoryId: string,
): URLSearchParams {
  const next = new URLSearchParams(current.toString());
  next.set("memoryId", memoryId);
  return next;
}

export function closeMemoryUrl(current: URLSearchParams): URLSearchParams {
  const next = new URLSearchParams(current.toString());
  next.delete("memoryId");
  return next;
}

function validTimestamp(value: unknown): string | null {
  if (typeof value !== "string") {
    return null;
  }
  return Number.isFinite(new Date(value).getTime()) ? value : null;
}

function normalizeExpiration(value: string): string {
  return value.trim();
}

function canonicalJson(value: unknown): string {
  if (Array.isArray(value)) {
    return `[${value.map(canonicalJson).join(",")}]`;
  }
  if (value !== null && typeof value === "object") {
    const object = value as Record<string, unknown>;
    return `{${Object.keys(object).sort().map((key) => (
      `${JSON.stringify(key)}:${canonicalJson(object[key])}`
    )).join(",")}}`;
  }
  return JSON.stringify(value);
}
