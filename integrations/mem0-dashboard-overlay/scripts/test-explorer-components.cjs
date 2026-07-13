#!/usr/bin/env node
"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { webcrypto } = require("node:crypto");
const { createRequire } = require("node:module");

function transpileModule(typescript, sourcePath, dependencies, jsx = false) {
  if (!fs.existsSync(sourcePath)) {
    throw new Error(`missing applied dashboard source: ${sourcePath}`);
  }
  const source = fs.readFileSync(sourcePath, "utf8");
  const compilerOptions = {
    esModuleInterop: true,
    module: typescript.ModuleKind.CommonJS,
    target: typescript.ScriptTarget.ES2022,
  };
  if (jsx) {
    compilerOptions.jsx = typescript.JsxEmit.ReactJSX;
  }
  const transpiled = typescript.transpileModule(source, {
    compilerOptions,
    fileName: sourcePath,
    reportDiagnostics: true,
  });
  const errors = (transpiled.diagnostics || []).filter(
    (diagnostic) => diagnostic.category === typescript.DiagnosticCategory.Error,
  );
  assert.deepEqual(errors, [], `${path.basename(sourcePath)} transpilation failed`);

  const module = { exports: {} };
  const localRequire = (specifier) => (
    Object.hasOwn(dependencies, specifier)
      ? dependencies[specifier]
      : dependencies.require(specifier)
  );
  new Function("exports", "module", "require", transpiled.outputText)(
    module.exports,
    module,
    localRequire,
  );
  return module.exports;
}

function loadModules(dashboardDir) {
  if (!globalThis.crypto) {
    globalThis.crypto = webcrypto;
  }
  const dashboardRequire = createRequire(path.join(dashboardDir, "package.json"));
  const typescript = dashboardRequire("typescript");
  const sourceRoot = path.join(dashboardDir, "src");
  const common = { require: dashboardRequire };
  const queryState = transpileModule(
    typescript,
    path.join(sourceRoot, "utils/explorer-query-state.ts"),
    common,
  );
  const componentStatePath = path.join(
    sourceRoot,
    "components/self-hosted/explorer/explorer-component-state.ts",
  );
  const componentState = transpileModule(typescript, componentStatePath, {
    ...common,
    "@/utils/explorer-query-state": queryState,
  });

  const React = dashboardRequire("react");
  const ui = createUiStubs(React);
  const dateRangeFilter = transpileModule(
    typescript,
    path.join(
      sourceRoot,
      "components/self-hosted/explorer/date-range-filter.tsx",
    ),
    {
      ...common,
      "@/components/ui/button": { Button: ui.Button },
      "@/components/ui/calendar": { Calendar: ui.Calendar },
      "@/components/ui/popover": ui.popover,
      "@/components/self-hosted/explorer/explorer-component-state": componentState,
      "@/utils/explorer-query-state": queryState,
    },
    true,
  );
  return {
    componentState,
    DateRangeFilter: dateRangeFilter.DateRangeFilter,
    React,
    ReactDOMServer: dashboardRequire("react-dom/server"),
  };
}

function createUiStubs(React) {
  function Button({ variant, size, asChild, ...props }) {
    return React.createElement("button", props);
  }
  function Calendar() {
    return null;
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
  return {
    Button,
    Calendar,
    popover: { Popover, PopoverContent, PopoverTrigger },
  };
}

function testServerRenderIsDeterministicAndNamesTheCurrentRange(modules) {
  const originalDateTimeFormat = Intl.DateTimeFormat;
  let implicitLocaleCalls = 0;
  Intl.DateTimeFormat = function DateTimeFormat(locale, options) {
    if (locale === undefined) {
      implicitLocaleCalls += 1;
    }
    return new originalDateTimeFormat("fr-FR", options);
  };
  let html;
  try {
    html = modules.ReactDOMServer.renderToStaticMarkup(
      modules.React.createElement(modules.DateRangeFilter, {
        value: {
          from: "2026-07-01T00:00:00.000Z",
          to: "2026-07-13T23:59:59.999Z",
        },
        onChange() {},
      }),
    );
  } finally {
    Intl.DateTimeFormat = originalDateTimeFormat;
  }

  assert.equal(implicitLocaleCalls, 0, "SSR must not use the host default locale");
  assert.match(html, />2026-07-01 – 2026-07-13<\/button>/);
  assert.match(
    html,
    /aria-label="Choose date range: 2026-07-01 – 2026-07-13"/,
  );
}

function testDateDraftsApplyUtcBoundariesAndRejectPartialRanges(state) {
  const selected = {
    from: new Date(2026, 6, 1),
    to: new Date(2026, 6, 13),
  };
  assert.deepEqual(state.calendarRangeToUtcRange(selected), {
    from: "2026-07-01T00:00:00.000Z",
    to: "2026-07-13T23:59:59.999Z",
  });
  assert.equal(
    state.calendarRangeToUtcRange({ from: selected.from, to: undefined }),
    null,
  );
  assert.deepEqual(
    state.isoRangeToCalendarRange({
      from: "2026-07-01T00:00:00.000Z",
      to: "2026-07-13T23:59:59.999Z",
    }),
    selected,
  );
}

function testFilterDraftOpenResetCancelApplyAndUniqueIds(state) {
  const appliedFilters = [
    {
      id: "duplicate",
      field: "metadata",
      operator: "contains",
      value: { key: "source", value: "codex" },
    },
    { id: "duplicate", field: "user_id", operator: "equals", value: "alice" },
    {
      id: "duplicate-duplicate-2",
      field: "run_id",
      operator: "equals",
      value: "run-1",
    },
  ];
  const original = structuredClone(appliedFilters);
  const opened = state.openFilterBuilderDraft("any", appliedFilters);
  assert.equal(opened.open, true);
  assert.equal(new Set(opened.filters.map((filter) => filter.id)).size, 3);
  assert.equal(opened.filters[0].id, "duplicate");
  assert.deepEqual(
    state.openFilterBuilderDraft("any", appliedFilters).filters.map(({ id }) => id),
    opened.filters.map(({ id }) => id),
    "deduplicated draft IDs must be stable",
  );

  opened.filters[0].value.value = "changed";
  const cancelled = state.cancelFilterBuilderDraft(opened);
  assert.equal(cancelled.open, false);
  assert.deepEqual(appliedFilters, original, "cancel must not mutate applied filters");

  const reset = state.openFilterBuilderDraft("all", appliedFilters);
  assert.equal(reset.match, "all");
  assert.equal(reset.filters[0].value.value, "codex");
  reset.filters[1] = {
    ...reset.filters[1],
    value: " alice ",
  };
  const applied = state.applyFilterBuilderDraft(reset);
  assert.equal(applied.draft.open, false);
  assert.equal(applied.match, "all");
  assert.equal(applied.filters[1].value, "alice");
  assert.deepEqual(applied.draft.filters, applied.filters);
}

function testFieldOperatorAndInEditorsResetCompatibleValues(state) {
  const original = {
    id: "filter-1",
    field: "user_id",
    operator: "in",
    value: ["alice"],
  };
  assert.deepEqual(state.changeExplorerFilterField(original, "metadata"), {
    id: "filter-1",
    field: "metadata",
    operator: "contains",
    value: { key: "", value: "" },
  });
  assert.deepEqual(state.changeExplorerFilterField(original, "category"), {
    id: "filter-1",
    field: "category",
    operator: "equals",
    value: "",
  });
  assert.deepEqual(state.changeExplorerFilterOperator(original, "not_equals"), {
    ...original,
    operator: "not_equals",
    value: "",
  });
  assert.deepEqual(
    state.parseCommaSeparatedFilterValues(" alice, , bob "),
    ["alice", "bob"],
  );
  assert.deepEqual(state.toggleFilterValue(["user"], "agent", true), [
    "user",
    "agent",
  ]);
  assert.deepEqual(state.toggleFilterValue(["user", "agent"], "user", false), [
    "agent",
  ]);
}

function testRemoveAllAndEntityClickPayloads(state) {
  const draft = state.openFilterBuilderDraft("all", [
    { id: "f-1", field: "category", operator: "equals", value: "work" },
  ]);
  const removed = state.removeAllFilterBuilderDraft(draft);
  assert.deepEqual(removed.filters, []);
  assert.deepEqual(removed.draft.filters, []);
  assert.equal(removed.draft.open, false);

  const identities = state.createEntityBadgeItems({
    userId: "user-12345678901234567890",
    agentId: null,
    appId: " ",
    runId: "run-1",
  });
  assert.deepEqual(identities.map(({ field }) => field), ["user_id", "run_id"]);
  assert.deepEqual(state.entityBadgeClickPayload(identities[0]), {
    field: "user_id",
    value: "user-12345678901234567890",
  });
  assert.equal(state.truncateIdentity(identities[0].value), "user-1234…567890");
}

function main() {
  if (process.argv.length !== 3) {
    throw new Error("usage: test-explorer-components.cjs <dashboard-dir>");
  }
  const modules = loadModules(path.resolve(process.argv[2]));
  testServerRenderIsDeterministicAndNamesTheCurrentRange(modules);
  testDateDraftsApplyUtcBoundariesAndRejectPartialRanges(modules.componentState);
  testFilterDraftOpenResetCancelApplyAndUniqueIds(modules.componentState);
  testFieldOperatorAndInEditorsResetCompatibleValues(modules.componentState);
  testRemoveAllAndEntityClickPayloads(modules.componentState);
  console.log("explorer components harness: 5 contracts passed");
}

try {
  main();
} catch (error) {
  console.error("explorer components harness failed:", error);
  process.exitCode = 1;
}
