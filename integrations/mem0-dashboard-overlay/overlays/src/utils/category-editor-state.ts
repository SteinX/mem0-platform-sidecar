import type { SidecarCategory } from "@/types/sidecar";
import {
  type CategoryField,
  editorToSchema,
  schemaToEditor,
} from "@/utils/category-schema";

export type EditorMode = "builder" | "advanced";

export type CategoryDraft = {
  name: string;
  description: string;
  enabled: boolean;
  strategy: string;
  mode: EditorMode;
  fields: CategoryField[];
  rawSchemaText: string;
  unsupportedPaths: string[];
};

export type BuilderTransition =
  | { status: "ready"; draft: CategoryDraft }
  | { status: "confirm"; unsupportedPaths: string[] }
  | { status: "invalid"; message: string };

const EMPTY_SCHEMA = { type: "object", properties: {} };

export function createCategoryDraft(category: SidecarCategory | null): CategoryDraft {
  const editor = schemaToEditor(category?.schema ?? EMPTY_SCHEMA);
  return {
    name: category?.name ?? "",
    description: category?.description ?? "",
    enabled: category?.enabled ?? true,
    strategy: category?.strategy ?? "metadata",
    mode: editor.mode,
    fields: editor.fields,
    rawSchemaText: editor.rawSchemaText,
    unsupportedPaths: editor.unsupportedPaths,
  };
}

export function parseAdvancedSchema(rawSchemaText: string): Record<string, unknown> {
  const parsed: unknown = JSON.parse(rawSchemaText);
  if (parsed === null || Array.isArray(parsed) || typeof parsed !== "object") {
    throw new Error("Advanced schema must be a JSON object.");
  }
  return parsed as Record<string, unknown>;
}

export function categoryDraftFingerprint(draft: CategoryDraft): string {
  return JSON.stringify({
    name: draft.name.trim(),
    description: draft.description,
    enabled: draft.enabled,
    strategy: draft.strategy,
    schema: activeSchemaFingerprint(draft),
  });
}

export function resolveCategorySchemaForSave(
  draft: CategoryDraft,
  initialDraft: CategoryDraft,
  originalSchema?: Record<string, unknown>,
): Record<string, unknown> {
  if (draft.mode === "advanced") {
    return parseAdvancedSchema(draft.rawSchemaText);
  }

  const generatedSchema = editorToSchema(draft.fields);
  if (
    originalSchema !== undefined
    && initialDraft.mode === "builder"
    && JSON.stringify(canonicalize(generatedSchema))
      === JSON.stringify(canonicalize(editorToSchema(initialDraft.fields)))
  ) {
    return originalSchema;
  }
  return generatedSchema;
}

export function planCategoryDisable(isDirty: boolean): "confirm" | "disable" {
  return isDirty ? "confirm" : "disable";
}

export function activateAdvancedMode(draft: CategoryDraft): CategoryDraft {
  if (draft.mode === "advanced") {
    return draft;
  }
  return {
    ...draft,
    mode: "advanced",
    rawSchemaText: JSON.stringify(editorToSchema(draft.fields), null, 2),
    unsupportedPaths: [],
  };
}

export function planBuilderTransition(draft: CategoryDraft): BuilderTransition {
  if (draft.mode === "builder") {
    return { status: "ready", draft };
  }
  try {
    const editor = schemaToEditor(parseAdvancedSchema(draft.rawSchemaText));
    if (editor.mode === "advanced") {
      return { status: "confirm", unsupportedPaths: editor.unsupportedPaths };
    }
    return {
      status: "ready",
      draft: {
        ...draft,
        mode: "builder",
        fields: editor.fields,
        unsupportedPaths: [],
      },
    };
  } catch (error) {
    return {
      status: "invalid",
      message: error instanceof Error ? error.message : String(error),
    };
  }
}

export function resetToEmptyBuilder(draft: CategoryDraft): CategoryDraft {
  return {
    ...draft,
    mode: "builder",
    fields: [],
    unsupportedPaths: [],
  };
}

function activeSchemaFingerprint(draft: CategoryDraft): unknown {
  if (draft.mode === "builder") {
    return canonicalize(editorToSchema(draft.fields));
  }
  try {
    return canonicalize(parseAdvancedSchema(draft.rawSchemaText));
  } catch {
    return { invalidAdvancedSchema: draft.rawSchemaText };
  }
}

function canonicalize(value: unknown): unknown {
  if (Array.isArray(value)) {
    return value.map(canonicalize);
  }
  if (value !== null && typeof value === "object") {
    return Object.fromEntries(
      Object.entries(value as Record<string, unknown>)
        .sort(([left], [right]) => left.localeCompare(right))
        .map(([key, item]) => [key, canonicalize(item)]),
    );
  }
  return value;
}
