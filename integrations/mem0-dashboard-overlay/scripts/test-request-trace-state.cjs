#!/usr/bin/env node
"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { createRequire } = require("node:module");

function loadState(dashboardDir) {
  const dashboardRequire = createRequire(
    path.join(dashboardDir, "package.json"),
  );
  const typescript = dashboardRequire("typescript");
  const statePath = path.join(dashboardDir, "src/utils/request-trace-state.ts");
  if (!fs.existsSync(statePath)) {
    throw new Error(`missing applied dashboard source: ${statePath}`);
  }
  const transpiled = typescript.transpileModule(
    fs.readFileSync(statePath, "utf8"),
    {
      compilerOptions: {
        module: typescript.ModuleKind.CommonJS,
        target: typescript.ScriptTarget.ES2022,
      },
      fileName: statePath,
      reportDiagnostics: true,
    },
  );
  const errors = (transpiled.diagnostics || []).filter(
    (diagnostic) => diagnostic.category === typescript.DiagnosticCategory.Error,
  );
  assert.deepEqual(errors, [], "request trace state transpilation failed");
  const module = { exports: {} };
  new Function("exports", "module", "require", transpiled.outputText)(
    module.exports,
    module,
    dashboardRequire,
  );
  return module.exports;
}

function filterAndPayloadContracts(state) {
  const base = {
    match: "any",
    filters: [
      {
        id: "user-old",
        field: "user_id",
        operator: "equals",
        value: " bob ",
      },
      { id: "agent", field: "agent_id", operator: "in", value: ["one", "two"] },
      {
        id: "category",
        field: "category",
        operator: "equals",
        value: "private",
      },
      {
        id: "user-new",
        field: "user_id",
        operator: "equals",
        value: " alice ",
      },
      { id: "run", field: "run_id", operator: "contains", value: " run-7 " },
    ],
    date_range: { from: "2026-07-01T00:00:00Z", to: "2026-07-13T00:00:00Z" },
    page: 999999,
    page_size: 20,
  };
  const query = state.normalizeRequestTraceQueryState(
    base,
    new URLSearchParams("operation=SEARCH&hasResults=true"),
  );
  assert.equal(query.match, "all", "Event API semantics are AND-only");
  assert.equal(
    state.normalizeRequestTraceQueryState(
      { ...base, page: 1 },
      new URLSearchParams("hasResults=false"),
    ).has_results,
    null,
    "unsupported hidden No Results state must normalize to Overview",
  );
  assert.equal(query.page, 250, "5000-record horizon must clamp deep links");
  assert.deepEqual(query.filters, [
    { id: "user-new", field: "user_id", operator: "equals", value: "alice" },
  ]);
  assert.deepEqual(state.requestTraceQueryPayload(query), {
    operation: "SEARCH",
    statuses: [],
    has_results: true,
    date_range: base.date_range,
    entity_filters: { user_id: "alice" },
    page: 250,
    page_size: 20,
  });
  assert.equal("match" in state.requestTraceQueryPayload(query), false);
  assert.equal("project_id" in state.requestTraceQueryPayload(query), false);
  assert.equal("app_id" in state.requestTraceQueryPayload(query), false);
  assert.equal(state.resetRequestTraceQueryPage(query).page, 1);
  assert.equal(query.page, 250, "reset helper must not mutate current state");
}

function urlContracts(state) {
  const initial = new URLSearchParams(
    "match=all&filters=%5B%7B%22field%22%3A%22user_id%22%7D%5D" +
      "&from=2026-07-01T00%3A00%3A00Z&page=7&operation=ADD" +
      "&hasResults=true&requestId=old%2Frequest&unknown=keep",
  );
  const opened = state.setTraceRequestIdInUrl(initial, "next/request");
  assert.equal(opened.get("requestId"), "next/request");
  assert.equal(opened.get("filters"), '[{"field":"user_id"}]');
  assert.equal(opened.get("operation"), "ADD");
  assert.equal(opened.get("hasResults"), "true");
  assert.equal(opened.get("page"), "7");
  assert.equal(opened.get("unknown"), "keep");
  const closed = state.closeTraceRequestUrl(opened);
  assert.equal(closed.get("requestId"), null);
  for (const key of ["filters", "operation", "hasResults", "page", "unknown"]) {
    assert.equal(
      closed.get(key),
      opened.get(key),
      `closing must preserve ${key}`,
    );
  }
  const controls = state.writeTraceControlUrl(initial, "GET_ALL", null);
  assert.equal(controls.get("operation"), "GET_ALL");
  assert.equal(controls.get("hasResults"), null);
  assert.equal(controls.get("requestId"), "old/request");
  assert.equal(controls.get("filters"), initial.get("filters"));
}

function independentControlContracts(state) {
  const combined = {
    match: "all",
    filters: [],
    date_range: { from: null, to: null },
    operation: "SEARCH",
    has_results: true,
    page: 9,
    page_size: 20,
  };

  const addWithResults = state.setRequestTraceOperation(combined, "ADD");
  assert.deepEqual(addWithResults, {
    ...combined,
    operation: "ADD",
    page: 1,
  });
  assert.equal(
    addWithResults.has_results,
    true,
    "operation changes must preserve the independent result filter",
  );

  const overviewWithResults = state.setRequestTraceOperation(combined, null);
  assert.equal(overviewWithResults.operation, null);
  assert.equal(overviewWithResults.has_results, true);
  assert.equal(overviewWithResults.page, 1);

  const searchWithoutResultFilter =
    state.toggleRequestTraceHasResults(combined);
  assert.equal(searchWithoutResultFilter.operation, "SEARCH");
  assert.equal(searchWithoutResultFilter.has_results, null);
  assert.equal(searchWithoutResultFilter.page, 1);

  const searchWithResults = state.toggleRequestTraceHasResults({
    ...combined,
    has_results: null,
  });
  assert.equal(searchWithResults.operation, "SEARCH");
  assert.equal(searchWithResults.has_results, true);
  assert.equal(searchWithResults.page, 1);

  const params = state.writeTraceControlUrl(
    new URLSearchParams("requestId=trace-1"),
    addWithResults.operation,
    addWithResults.has_results,
  );
  assert.equal(params.get("operation"), "ADD");
  assert.equal(params.get("hasResults"), "true");
  assert.equal(params.get("requestId"), "trace-1");

  assert.deepEqual(state.requestTraceQueryPayload(addWithResults), {
    operation: "ADD",
    statuses: [],
    has_results: true,
    date_range: combined.date_range,
    entity_filters: {},
    page: 1,
    page_size: 20,
  });
}

function generationContracts(state) {
  const listGeneration = state.nextTraceRequestGeneration(8);
  assert.equal(listGeneration, 9);
  assert.equal(state.isCurrentTraceListRequest(9, listGeneration, true), true);
  assert.equal(state.isCurrentTraceListRequest(8, listGeneration, true), false);
  assert.equal(
    state.isCurrentTraceListRequest(9, listGeneration, false),
    false,
  );

  const detail = state.beginTraceDetailRequest(11, "event/a");
  assert.deepEqual(detail, { generation: 12, targetId: "event/a" });
  assert.equal(
    state.canApplyTraceDetailRequest(detail, 12, "event/a", true),
    true,
  );
  assert.equal(
    state.canApplyTraceDetailRequest(detail, 12, "event/b", true),
    false,
  );
  assert.equal(
    state.canApplyTraceDetailRequest(detail, 13, "event/a", true),
    false,
  );
  assert.equal(
    state.canApplyTraceDetailRequest(detail, 12, "event/a", false),
    false,
  );
  assert.equal(
    state.canApplyTraceDetailRequest(
      { generation: 21, targetId: "same-event" },
      23,
      "same-event",
      true,
    ),
    false,
    "closing and reopening the same ID must reject old clipboard/detail work",
  );
}

function pageBoundaryContracts(state) {
  assert.equal(state.normalizeTracePage(0), 1);
  assert.equal(state.normalizeTracePage("bad"), 1);
  assert.equal(state.normalizeTracePage(1), 1);
  assert.equal(state.normalizeTracePage(250), 250);
  assert.equal(state.normalizeTracePage(251), 250);
  assert.equal(state.normalizeTracePage(Number.MAX_SAFE_INTEGER), 250);
  assert.equal(
    state.normalizeTracePage(999, 30),
    167,
    "page 167 still starts before the 5000-record horizon",
  );
}

function main() {
  if (process.argv.length !== 3) {
    throw new Error("usage: test-request-trace-state.cjs <dashboard-dir>");
  }
  const dashboardDir = path.resolve(process.argv[2]);
  const state = loadState(dashboardDir);
  filterAndPayloadContracts(state);
  urlContracts(state);
  independentControlContracts(state);
  generationContracts(state);
  pageBoundaryContracts(state);
  console.log("request trace state harness: 5 contract groups passed");
}

main();
