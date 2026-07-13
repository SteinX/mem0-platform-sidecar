#!/usr/bin/env node
"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { webcrypto } = require("node:crypto");
const { createRequire } = require("node:module");

function loadExplorerQueryStateModule(dashboardDir) {
  if (!globalThis.crypto) {
    globalThis.crypto = webcrypto;
  }

  const dashboardRequire = createRequire(
    path.join(dashboardDir, "package.json"),
  );
  const typescript = dashboardRequire("typescript");
  const statePath = path.join(
    __dirname,
    "../overlays/src/utils/explorer-query-state.ts",
  );
  const source = fs.readFileSync(statePath, "utf8");
  const transpiled = typescript.transpileModule(source, {
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
  assert.deepEqual(errors, [], "explorer query state transpilation failed");

  const module = { exports: {} };
  new Function(
    "exports",
    "module",
    "require",
    "URLSearchParams",
    "crypto",
    transpiled.outputText,
  )(
    module.exports,
    module,
    dashboardRequire,
    URLSearchParams,
    globalThis.crypto,
  );
  return module.exports;
}

function testExactRuntimeExports(state) {
  assert.deepEqual(Object.keys(state).sort(), [
    "createExplorerFilter",
    "datePresetRange",
    "normalizeExplorerFilters",
    "readExplorerUrlState",
    "writeExplorerUrlState",
  ]);
}

function testBlankRowsAreRemovedAndIdsStayStable(state) {
  const created = state.createExplorerFilter();
  assert.equal(typeof created.id, "string");
  assert.notEqual(created.id, "");
  assert.deepEqual(
    { field: created.field, operator: created.operator, value: created.value },
    { field: "user_id", operator: "equals", value: "" },
  );

  const normalized = state.normalizeExplorerFilters([
    created,
    { id: "kept", field: "category", operator: "equals", value: " work " },
    { id: "blank-in", field: "run_id", operator: "in", value: [" "] },
    {
      id: "metadata",
      field: "metadata",
      operator: "contains",
      value: { key: " source ", value: " codex " },
    },
  ]);

  assert.deepEqual(normalized, [
    { id: "kept", field: "category", operator: "equals", value: "work" },
    {
      id: "metadata",
      field: "metadata",
      operator: "contains",
      value: { key: "source", value: "codex" },
    },
  ]);
  assert.deepEqual(state.normalizeExplorerFilters(normalized), normalized);
}

function testMatchModesArePreserved(state) {
  for (const match of ["all", "any"]) {
    const initial = new URLSearchParams({ match });
    const read = state.readExplorerUrlState(initial);
    assert.equal(read.match, match);

    const written = state.writeExplorerUrlState(initial, read);
    assert.equal(written.get("match"), match);
    assert.equal(state.readExplorerUrlState(written).match, match);
  }
}

function testUtcDatePresetsUseInjectedNow(state) {
  const now = new Date("2026-07-13T12:34:56.789Z");
  assert.deepEqual(state.datePresetRange("all", now), {
    from: null,
    to: null,
  });
  assert.deepEqual(state.datePresetRange("1d", now), {
    from: "2026-07-12T12:34:56.789Z",
    to: "2026-07-13T12:34:56.789Z",
  });
  assert.deepEqual(state.datePresetRange("7d", now), {
    from: "2026-07-06T12:34:56.789Z",
    to: "2026-07-13T12:34:56.789Z",
  });
  assert.deepEqual(state.datePresetRange("30d", now), {
    from: "2026-06-13T12:34:56.789Z",
    to: "2026-07-13T12:34:56.789Z",
  });
}

function testMalformedFieldsFallBackIndependently(state) {
  const params = new URLSearchParams({
    match: "neither",
    filters: "{bad json",
    from: "not-a-date",
    to: "2026-07-13T12:34:56.789Z",
    page: "0",
    memoryId: "mem/42",
    requestId: "req-7",
    entityType: "user",
    entityId: "alice",
  });

  assert.deepEqual(state.readExplorerUrlState(params), {
    match: "all",
    filters: [],
    date_range: { from: null, to: "2026-07-13T12:34:56.789Z" },
    page: 1,
    page_size: 20,
    sort: "created_at_desc",
    memoryId: "mem/42",
    requestId: "req-7",
    entityType: "user",
    entityId: "alice",
  });
}

function testRoundTripRetainsDrawerAndUnknownParamsWithoutChangingPath(state) {
  const url = new URL(
    "https://dashboard.test/dashboard?memoryId=mem%2F42&requestId=req-7"
      + "&entityType=agent&entityId=agent-9&tab=raw",
  );
  const nextState = {
    match: "any",
    filters: [
      { id: "f-2", field: "category", operator: "equals", value: "work" },
      { id: "f-1", field: "user_id", operator: "equals", value: "alice" },
    ],
    date_range: {
      from: "2026-07-06T12:34:56.789Z",
      to: "2026-07-13T12:34:56.789Z",
    },
    page: 3,
    page_size: 20,
    sort: "created_at_desc",
  };

  const written = state.writeExplorerUrlState(url.searchParams, nextState);
  url.search = written.toString();

  assert.equal(url.pathname, "/dashboard");
  assert.equal(written.get("memoryId"), "mem/42");
  assert.equal(written.get("requestId"), "req-7");
  assert.equal(written.get("entityType"), "agent");
  assert.equal(written.get("entityId"), "agent-9");
  assert.equal(written.get("tab"), "raw");
  assert.equal(written.has("project_id"), false);
  assert.deepEqual(JSON.parse(written.get("filters")), nextState.filters);
  assert.deepEqual(state.readExplorerUrlState(written), {
    ...nextState,
    memoryId: "mem/42",
    requestId: "req-7",
    entityType: "agent",
    entityId: "agent-9",
  });
}

function main() {
  if (process.argv.length !== 3) {
    throw new Error("usage: test-explorer-query-state.cjs <dashboard-dir>");
  }

  const state = loadExplorerQueryStateModule(path.resolve(process.argv[2]));
  testExactRuntimeExports(state);
  testBlankRowsAreRemovedAndIdsStayStable(state);
  testMatchModesArePreserved(state);
  testUtcDatePresetsUseInjectedNow(state);
  testMalformedFieldsFallBackIndependently(state);
  testRoundTripRetainsDrawerAndUnknownParamsWithoutChangingPath(state);
  console.log("explorer query state harness: 6 contracts passed");
}

try {
  main();
} catch (error) {
  console.error(error);
  process.exitCode = 1;
}
