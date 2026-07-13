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

function testFiltersMustMatchBackendContract(state) {
  const filters = [
    { id: " equals ", field: "category", operator: "equals", value: " work " },
    {
      id: "not-equals",
      field: "memory_id",
      operator: "not_equals",
      value: " mem-1 ",
    },
    {
      id: "in",
      field: "user_id",
      operator: "in",
      value: [" alice ", "bob"],
    },
    {
      id: "entity",
      field: "entity_type",
      operator: "equals",
      value: " agent ",
    },
    {
      id: "entity-in",
      field: "entity_type",
      operator: "in",
      value: ["user", " run "],
    },
    {
      id: "metadata",
      field: "metadata",
      operator: "contains",
      value: { key: " source ", value: " codex " },
    },
    {
      id: "metadata-string",
      field: "metadata",
      operator: "contains",
      value: "source",
    },
    {
      id: "metadata-extra-key",
      field: "metadata",
      operator: "contains",
      value: { key: "source", value: "codex", extra: true },
    },
    {
      id: "metadata-equals",
      field: "metadata",
      operator: "equals",
      value: { key: "source", value: "codex" },
    },
    {
      id: "scalar-contains",
      field: "category",
      operator: "contains",
      value: "work",
    },
    {
      id: "equals-array",
      field: "category",
      operator: "equals",
      value: ["work"],
    },
    { id: "in-string", field: "user_id", operator: "in", value: "alice" },
    {
      id: "partial-in",
      field: "user_id",
      operator: "in",
      value: ["alice", " "],
    },
    {
      id: "non-string-in",
      field: "run_id",
      operator: "in",
      value: ["run-1", 7],
    },
    {
      id: "bad-entity",
      field: "entity_type",
      operator: "equals",
      value: "team",
    },
    {
      id: "bad-entity-in",
      field: "entity_type",
      operator: "in",
      value: ["user", "team"],
    },
  ];
  const original = structuredClone(filters);

  assert.deepEqual(state.normalizeExplorerFilters(filters), [
    { id: " equals ", field: "category", operator: "equals", value: "work" },
    {
      id: "not-equals",
      field: "memory_id",
      operator: "not_equals",
      value: "mem-1",
    },
    {
      id: "in",
      field: "user_id",
      operator: "in",
      value: ["alice", "bob"],
    },
    {
      id: "entity",
      field: "entity_type",
      operator: "equals",
      value: "agent",
    },
    {
      id: "entity-in",
      field: "entity_type",
      operator: "in",
      value: ["user", "run"],
    },
    {
      id: "metadata",
      field: "metadata",
      operator: "contains",
      value: { key: "source", value: "codex" },
    },
  ]);
  assert.deepEqual(filters, original, "normalization must not mutate input");
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

function testStrictDateRangesRejectInvalidAndReversedValues(state) {
  const valid = "2026-07-13T12:34:56.789Z";
  assert.deepEqual(
    state.readExplorerUrlState(new URLSearchParams({
      from: "2024-02-29T12:00:00+05:30",
      to: "2024-02-29T07:00:00Z",
    })).date_range,
    {
      from: "2024-02-29T12:00:00+05:30",
      to: "2024-02-29T07:00:00Z",
    },
  );
  const invalidValues = [
    "2026-02-30T12:00:00Z",
    "2026-07-13T24:00:00Z",
    "2026-07-13T12:60:00Z",
    "2026-07-13T12:00:60Z",
    "2026-07-13T12:00:00+24:00",
    "2026-07-13T12:00:00+02:60",
    "2026-07-13T12:00:00",
  ];

  for (const from of invalidValues) {
    assert.deepEqual(
      state.readExplorerUrlState(new URLSearchParams({ from, to: valid }))
        .date_range,
      { from: null, to: valid },
      from,
    );
  }
  assert.deepEqual(
    state.readExplorerUrlState(new URLSearchParams({
      from: valid,
      to: "2026-02-30T12:00:00Z",
    })).date_range,
    { from: valid, to: null },
  );
  assert.deepEqual(
    state.readExplorerUrlState(new URLSearchParams({
      from: "2026-07-14T01:00:00+02:00",
      to: "2026-07-13T22:59:59Z",
    })).date_range,
    { from: null, to: null },
  );
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
  testFiltersMustMatchBackendContract(state);
  testMatchModesArePreserved(state);
  testUtcDatePresetsUseInjectedNow(state);
  testMalformedFieldsFallBackIndependently(state);
  testStrictDateRangesRejectInvalidAndReversedValues(state);
  testRoundTripRetainsDrawerAndUnknownParamsWithoutChangingPath(state);
  console.log("explorer query state harness: 8 contracts passed");
}

try {
  main();
} catch (error) {
  console.error(error);
  process.exitCode = 1;
}
