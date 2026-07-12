#!/usr/bin/env node
"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { webcrypto } = require("node:crypto");
const { createRequire } = require("node:module");

function transpileModule(dashboardDir, relativePath, runtimeRequire) {
  const dashboardRequire = createRequire(path.join(dashboardDir, "package.json"));
  const typescript = dashboardRequire("typescript");
  const sourcePath = path.join(dashboardDir, relativePath);
  const source = fs.readFileSync(sourcePath, "utf8");
  const transpiled = typescript.transpileModule(source, {
    compilerOptions: {
      module: typescript.ModuleKind.CommonJS,
      target: typescript.ScriptTarget.ES2022,
    },
    fileName: sourcePath,
    reportDiagnostics: true,
  });
  const errors = (transpiled.diagnostics || []).filter(
    (diagnostic) => diagnostic.category === typescript.DiagnosticCategory.Error,
  );
  assert.deepEqual(errors, [], `${relativePath} transpilation failed`);

  const module = { exports: {} };
  new Function("exports", "module", "require", transpiled.outputText)(
    module.exports,
    module,
    runtimeRequire || dashboardRequire,
  );
  return module.exports;
}

function loadModules(dashboardDir) {
  if (!globalThis.crypto) {
    globalThis.crypto = webcrypto;
  }
  const dashboardRequire = createRequire(path.join(dashboardDir, "package.json"));
  const schema = transpileModule(
    dashboardDir,
    "src/utils/category-schema.ts",
    dashboardRequire,
  );
  const state = transpileModule(
    dashboardDir,
    "src/utils/category-editor-state.ts",
    (specifier) => specifier === "@/utils/category-schema"
      ? schema
      : dashboardRequire(specifier),
  );
  return { schema, state };
}

function category(schema) {
  return {
    id: "category-1",
    project_id: "project-1",
    name: "Status",
    description: "Workflow status",
    schema,
    enabled: true,
    strategy: "metadata",
    version: 1,
    created_at: "2026-07-11T00:00:00Z",
    updated_at: "2026-07-11T00:00:00Z",
  };
}

function supportedSchema() {
  return {
    type: "object",
    properties: {
      status: { type: "string", enum: ["open", "closed"], default: "open" },
    },
    required: ["status"],
  };
}

function testActiveAdvancedIsNoOp(state) {
  const draft = state.createCategoryDraft(category({
    type: "object",
    additionalProperties: false,
    properties: { status: { type: "string" } },
  }));
  const rawSchemaText = draft.rawSchemaText;

  const next = state.activateAdvancedMode(draft);

  assert.strictEqual(next, draft);
  assert.equal(next.rawSchemaText, rawSchemaText);
}

function testViewRoundTripRemainsClean(state) {
  const draft = state.createCategoryDraft(category(supportedSchema()));
  const initialFingerprint = state.categoryDraftFingerprint(draft);
  const advanced = state.activateAdvancedMode(draft);
  const transition = state.planBuilderTransition(advanced);

  assert.equal(transition.status, "ready");
  const viewOnlyChanges = {
    ...transition.draft,
    rawSchemaText: "inactive view text",
    unsupportedPaths: ["$.view-only"],
  };
  assert.equal(
    state.categoryDraftFingerprint(viewOnlyChanges),
    initialFingerprint,
  );
}

function testPersistedChangesBecomeDirty(state) {
  const draft = state.createCategoryDraft(category(supportedSchema()));
  const changed = { ...draft, name: "Changed status" };

  assert.notEqual(
    state.categoryDraftFingerprint(changed),
    state.categoryDraftFingerprint(draft),
  );
}

function testInvalidAdvancedEditBecomesDirty(state) {
  const draft = state.createCategoryDraft(category(supportedSchema()));
  const advanced = state.activateAdvancedMode(draft);
  const invalid = { ...advanced, rawSchemaText: "{" };

  assert.notEqual(
    state.categoryDraftFingerprint(invalid),
    state.categoryDraftFingerprint(draft),
  );
}

function testEnumDefaultMembership(schema) {
  const field = schema.createEmptyField();
  Object.assign(field, {
    key: "status",
    type: "enum",
    enumValues: ["open", "closed"],
    hasDefault: true,
    defaultValue: "unknown",
  });

  const validation = schema.validateCategoryFields([field]);
  assert.equal(
    validation.fieldErrors[field.id],
    "Default must be one of the enum options",
  );
}

function testBasicsOnlyEditPreservesOriginalEmptySchema(schema, state) {
  const originalSchema = {};
  const initialDraft = state.createCategoryDraft(category(originalSchema));
  const renamedDraft = { ...initialDraft, name: "Renamed status" };

  assert.deepEqual(
    state.resolveCategorySchemaForSave(
      renamedDraft,
      initialDraft,
      originalSchema,
    ),
    originalSchema,
  );

  const field = schema.createEmptyField();
  Object.assign(field, { key: "status", type: "string" });
  const schemaEditedDraft = { ...renamedDraft, fields: [field] };
  assert.deepEqual(
    state.resolveCategorySchemaForSave(
      schemaEditedDraft,
      initialDraft,
      originalSchema,
    ),
    {
      type: "object",
      properties: { status: { type: "string" } },
    },
  );
}

function testLosslessBuilderEditsAreNotMistakenForUnchanged(schema, state) {
  const originalSchema = {
    type: "object",
    properties: {
      status: {
        type: "string",
        title: "   ",
        enum: ["open", ""],
      },
    },
  };
  const initialDraft = state.createCategoryDraft(category(originalSchema));
  assert.equal(initialDraft.mode, "builder");
  const editedField = {
    ...initialDraft.fields[0],
    title: "",
    enumValues: ["open"],
  };
  const editedDraft = { ...initialDraft, fields: [editedField] };

  assert.deepEqual(
    state.resolveCategorySchemaForSave(
      editedDraft,
      initialDraft,
      originalSchema,
    ),
    {
      type: "object",
      properties: {
        status: { type: "string", enum: ["open"] },
      },
    },
  );
}

function testDirtyDisableRequiresConfirmation(state) {
  assert.equal(state.planCategoryDisable(false), "disable");
  assert.equal(state.planCategoryDisable(true), "confirm");
}

function testBooleanDefaultTogglePersistsDisplayedFalse(schema) {
  const field = schema.createEmptyField();
  Object.assign(field, { key: "active", type: "boolean" });

  const enabled = schema.setFieldDefaultEnabled(field, true);
  assert.equal(enabled.hasDefault, true);
  assert.equal(enabled.defaultValue, "false");
  assert.equal(schema.validateCategoryFields([enabled]).valid, true);

  const disabled = schema.setFieldDefaultEnabled(enabled, false);
  assert.equal(disabled.hasDefault, false);
  assert.equal(disabled.defaultValue, "false");

  const textWithEmptyDefault = schema.createEmptyField();
  Object.assign(textWithEmptyDefault, {
    key: "active",
    type: "string",
    hasDefault: true,
  });
  const changedType = schema.setFieldType(textWithEmptyDefault, "boolean");
  assert.equal(changedType.type, "boolean");
  assert.equal(changedType.defaultValue, "false");
  assert.equal(schema.validateCategoryFields([changedType]).valid, true);
}

function main() {
  if (process.argv.length !== 3) {
    throw new Error("usage: test-category-editor-state.cjs <dashboard-dir>");
  }

  const { schema, state } = loadModules(path.resolve(process.argv[2]));
  testActiveAdvancedIsNoOp(state);
  testViewRoundTripRemainsClean(state);
  testPersistedChangesBecomeDirty(state);
  testInvalidAdvancedEditBecomesDirty(state);
  testEnumDefaultMembership(schema);
  testBasicsOnlyEditPreservesOriginalEmptySchema(schema, state);
  testLosslessBuilderEditsAreNotMistakenForUnchanged(schema, state);
  testDirtyDisableRequiresConfirmation(state);
  testBooleanDefaultTogglePersistsDisplayedFalse(schema);
  console.log("category editor state harness: 9 contracts passed");
}

try {
  main();
} catch (error) {
  console.error(error);
  process.exitCode = 1;
}
