#!/usr/bin/env node
"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { createRequire } = require("node:module");

function loadState(dashboardDir) {
  const dashboardRequire = createRequire(path.join(dashboardDir, "package.json"));
  const typescript = dashboardRequire("typescript");
  const statePath = path.join(dashboardDir, "src/utils/memory-explorer-state.ts");
  if (!fs.existsSync(statePath)) {
    throw new Error(`missing applied dashboard source: ${statePath}`);
  }
  const transpiled = typescript.transpileModule(fs.readFileSync(statePath, "utf8"), {
    compilerOptions: {
      module: typescript.ModuleKind.CommonJS,
      target: typescript.ScriptTarget.ES2022,
    },
    fileName: statePath,
    reportDiagnostics: true,
  });
  const errors = (transpiled.diagnostics || []).filter(
    (diagnostic) => diagnostic.category === typescript.DiagnosticCategory.Error,
  );
  assert.deepEqual(errors, [], "memory explorer state transpilation failed");
  const module = { exports: {} };
  new Function("exports", "module", "require", transpiled.outputText)(
    module.exports,
    module,
    dashboardRequire,
  );
  return module.exports;
}

function loadMemoryCategories(dashboardDir) {
  const dashboardRequire = createRequire(path.join(dashboardDir, "package.json"));
  const typescript = dashboardRequire("typescript");
  const React = dashboardRequire("react");
  const sourcePath = path.join(
    dashboardDir,
    "src/app/(root)/dashboard/memories/memory-categories.tsx",
  );
  if (!fs.existsSync(sourcePath)) {
    throw new Error(`missing applied dashboard source: ${sourcePath}`);
  }
  const transpiled = typescript.transpileModule(fs.readFileSync(sourcePath, "utf8"), {
    compilerOptions: {
      esModuleInterop: true,
      jsx: typescript.JsxEmit.ReactJSX,
      module: typescript.ModuleKind.CommonJS,
      target: typescript.ScriptTarget.ES2022,
    },
    fileName: sourcePath,
    reportDiagnostics: true,
  });
  const errors = (transpiled.diagnostics || []).filter(
    (diagnostic) => diagnostic.category === typescript.DiagnosticCategory.Error,
  );
  assert.deepEqual(errors, [], "memory categories transpilation failed");
  function Button({ asChild, ...props }) {
    return React.createElement("button", props);
  }
  function Popover({ children }) {
    return React.createElement(React.Fragment, null, children);
  }
  function PopoverTrigger({ children }) {
    return children;
  }
  function PopoverContent({ children, ...props }) {
    return React.createElement("div", props, children);
  }
  const module = { exports: {} };
  const localRequire = (specifier) => {
    if (specifier === "@/components/ui/button") return { Button };
    if (specifier === "@/components/ui/popover") {
      return { Popover, PopoverContent, PopoverTrigger };
    }
    return dashboardRequire(specifier);
  };
  new Function("exports", "module", "require", transpiled.outputText)(
    module.exports,
    module,
    localRequire,
  );
  return {
    MemoryCategories: module.exports.MemoryCategories,
    React,
    ReactDOMServer: dashboardRequire("react-dom/server"),
  };
}

function queryContracts(state) {
  const query = {
    match: "all",
    filters: [{ id: "ui-1", field: "user_id", operator: "equals", value: "alice" }],
    date_range: { from: null, to: null },
    page: 4,
    page_size: 20,
    sort: "created_at_desc",
  };
  assert.deepEqual(state.memoryQueryPayload(query), {
    ...query,
    filters: [{ field: "user_id", operator: "equals", value: "alice" }],
  });
  assert.equal(state.resetMemoryQueryPage(query).page, 1);
  assert.equal(query.page, 4, "query helper must not mutate URL state");
  assert.equal(state.memoryQueriesEqual(query, structuredClone(query)), true);
  assert.equal(state.memoryQueriesEqual(query, { ...query, page: 3 }), false);
}

function requestGenerationContracts(state) {
  const generation = state.nextMemoryRequestGeneration(8);
  assert.equal(generation, 9);
  assert.equal(state.isCurrentMemoryRequest(9, generation), true);
  assert.equal(state.isCurrentMemoryRequest(8, generation), false);
}

function operationGuardContracts(state) {
  const save = state.beginMemoryOperation(11, "mem-a");
  assert.deepEqual(save, { generation: 12, targetId: "mem-a" });
  assert.equal(state.canApplyMemoryOperation(save, 12, "mem-a", true), true);
  assert.equal(
    state.canApplyMemoryOperation(save, 12, "mem-b", true),
    false,
    "save completion must not update a newly selected drawer",
  );
  assert.equal(
    state.canApplyMemoryOperation(save, 12, "mem-a", false),
    false,
    "save completion must not update an unmounted drawer",
  );
  assert.equal(
    state.canApplyMemoryOperation(save, 13, "mem-a", true),
    false,
    "invalidated mutation generations must be ignored",
  );
  const deletion = state.beginMemoryOperation(save.generation, "mem-a");
  assert.equal(
    state.canApplyMemoryOperation(deletion, deletion.generation, "mem-b", true),
    false,
    "delete completion must not close a newly selected drawer",
  );
  assert.equal(
    state.canApplyMemoryOperation(deletion, deletion.generation, "mem-a", false),
    false,
    "delete completion must not close an unmounted drawer",
  );
}

function paginationAndMemoryIdContracts(state) {
  assert.equal(state.normalizeMemoryId(" mem/one "), "mem/one");
  assert.equal(state.normalizeMemoryId("   "), null);
  assert.equal(state.normalizeMemoryId(null), null);
  assert.equal(state.shouldShowMemoryPagination(1, false), false);
  assert.equal(state.shouldShowMemoryPagination(1, true), true);
  assert.equal(
    state.shouldShowMemoryPagination(2, false),
    true,
    "an empty out-of-range page must still expose Previous",
  );
}

function memoryCategoriesContracts(modules) {
  const desktop = modules.ReactDOMServer.renderToStaticMarkup(
    modules.React.createElement(modules.MemoryCategories, {
      categories: ["work", "decision", "private"],
    }),
  );
  assert.match(desktop, /aria-expanded="false"/);
  assert.match(desktop, /aria-label="Show 2 more categories"/);
  assert.match(desktop, />\+2<\/button>/);

  const mobile = modules.ReactDOMServer.renderToStaticMarkup(
    modules.React.createElement(modules.MemoryCategories, {
      categories: ["work", "decision", "work", " "],
      mobile: true,
    }),
  );
  assert.doesNotMatch(mobile, /<button/);
  assert.match(mobile, />work</);
  assert.match(mobile, />decision</);
  assert.equal((mobile.match(/>work</g) || []).length, 1);
}

function draftContracts(state) {
  const memory = {
    id: "mem/one",
    memory: "before",
    metadata: { nested: { ok: true } },
    expiration_date: null,
  };
  const initialized = state.initializeMemoryDraft(memory);
  assert.equal(initialized.text, "before");
  assert.match(initialized.metadataText, /"nested"/);
  assert.equal(state.isMemoryDraftDirty(initialized, initialized), false);
  assert.equal(
    state.isMemoryDraftDirty({ ...initialized, text: "after" }, initialized),
    true,
  );
  assert.equal(state.isMemoryDraftReady("mem/one", "mem/one", null), false);
  assert.equal(state.isMemoryDraftReady("mem/one", "mem/one", "mem/one"), true);
  assert.equal(state.isMemoryDraftReady("mem/two", "mem/one", "mem/one"), false);
}

function metadataContracts(state) {
  assert.deepEqual(state.parseMemoryMetadataObject('{"ok":true}'), { ok: true });
  for (const value of ["[]", "null", '"text"', "{bad"] ) {
    assert.throws(() => state.parseMemoryMetadataObject(value));
  }
}

function patchContracts(state) {
  const initial = { text: "before", metadataText: '{"x":1}', expiration: "" };
  assert.deepEqual(state.buildMemoryPatch(initial, initial), {});
  assert.deepEqual(
    state.buildMemoryPatch(
      { text: "after", metadataText: '{"x":2}', expiration: "2027-01-01T00:00:00Z" },
      initial,
    ),
    { text: "after", metadata: { x: 2 }, expiration_date: "2027-01-01T00:00:00Z" },
  );
  assert.deepEqual(
    state.buildMemoryPatch({ ...initial, expiration: "" }, { ...initial, expiration: "x" }),
    { expiration_date: null },
  );
  assert.deepEqual(
    state.buildMemoryPatch(
      { ...initial, expiration: "  2027-01-01T00:00:00Z  " },
      { ...initial, expiration: "2027-01-01T00:00:00Z" },
    ),
    {},
  );
}

function historyContracts(state) {
  const parsed = state.parseMemoryHistory([
    {
      event: "UPDATE",
      input: [
        { role: "user", content: "Remember this" },
        { role: 7, content: null },
      ],
      old_memory: "old",
      new_memory: "new",
      updated_at: "2026-07-13T12:00:00Z",
    },
    { event: 42, input: "bad", created_at: "not-a-date" },
    null,
    "malformed entry",
  ]);
  assert.deepEqual(parsed.sourceMessages, [{ role: "user", content: "Remember this" }]);
  assert.equal(parsed.updates[0].event, "UPDATE");
  assert.equal(parsed.updates[0].oldMemory, "old");
  assert.equal(parsed.updates[0].newMemory, "new");
  assert.equal(parsed.updates[1].event, "Unknown update");
  assert.equal(parsed.updates[1].timestamp, null);
  assert.equal(parsed.updates.length, 2, "malformed history entries are ignored");
  assert.deepEqual(state.parseMemoryHistory([]).sourceMessages, []);
}

function deleteAndUrlContracts(state) {
  assert.equal(state.pageAfterMemoryDelete(3, 1), 2);
  assert.equal(state.pageAfterMemoryDelete(3, 1, false), 3);
  assert.equal(state.pageAfterMemoryDelete(3, 2), 3);
  assert.equal(state.pageAfterMemoryDelete(1, 1), 1);
  assert.equal(state.memoryApiPath("mem/one"), "/v1/memories/mem%2Fone");
  const params = new URLSearchParams("unknown=keep&drawer=open&memoryId=mem%2Fone&page=2");
  assert.equal(state.setMemoryIdInUrl(params, "next/id").get("memoryId"), "next/id");
  const closed = state.closeMemoryUrl(params);
  assert.equal(closed.get("memoryId"), null);
  assert.equal(closed.get("unknown"), "keep");
  assert.equal(closed.get("drawer"), "open");
  assert.equal(closed.get("page"), "2");
  const deletion = state.memoryDeleteNavigation(params, 3, 1);
  assert.equal(deletion.page, 2);
  assert.equal(deletion.searchParams.get("memoryId"), null);
  assert.equal(deletion.searchParams.get("unknown"), "keep");
  assert.equal(deletion.searchParams.get("drawer"), "open");
  assert.equal(state.memoryDeleteNavigation(params, 3, 1, false).page, 3);
}

function exactExports(state) {
  assert.deepEqual(Object.keys(state).sort(), [
    "beginMemoryOperation",
    "buildMemoryPatch",
    "canApplyMemoryOperation",
    "closeMemoryUrl",
    "initializeMemoryDraft",
    "isCurrentMemoryRequest",
    "isMemoryDraftDirty",
    "isMemoryDraftReady",
    "memoryApiPath",
    "memoryDeleteNavigation",
    "memoryQueriesEqual",
    "memoryQueryPayload",
    "nextMemoryRequestGeneration",
    "normalizeMemoryId",
    "pageAfterMemoryDelete",
    "parseMemoryHistory",
    "parseMemoryMetadataObject",
    "resetMemoryQueryPage",
    "setMemoryIdInUrl",
    "shouldShowMemoryPagination",
  ]);
}

function main() {
  if (process.argv.length !== 3) {
    throw new Error("usage: test-memory-explorer-state.cjs <dashboard-dir>");
  }
  const dashboardDir = path.resolve(process.argv[2]);
  const state = loadState(dashboardDir);
  const categories = loadMemoryCategories(dashboardDir);
  queryContracts(state);
  requestGenerationContracts(state);
  operationGuardContracts(state);
  paginationAndMemoryIdContracts(state);
  draftContracts(state);
  metadataContracts(state);
  patchContracts(state);
  historyContracts(state);
  deleteAndUrlContracts(state);
  exactExports(state);
  memoryCategoriesContracts(categories);
  console.log("memory explorer state harness: 11 contracts passed");
}

try {
  main();
} catch (error) {
  console.error("memory explorer state harness failed:", error);
  process.exitCode = 1;
}
