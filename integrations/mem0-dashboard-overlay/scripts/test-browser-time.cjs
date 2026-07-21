#!/usr/bin/env node
"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { createRequire } = require("node:module");

function loadBrowserTime(dashboardDir) {
  const dashboardRequire = createRequire(
    path.join(dashboardDir, "package.json"),
  );
  const typescript = dashboardRequire("typescript");
  const sourcePath = path.join(dashboardDir, "src/utils/browser-time.ts");
  if (!fs.existsSync(sourcePath)) {
    throw new Error(`missing applied dashboard source: ${sourcePath}`);
  }
  const transpiled = typescript.transpileModule(
    fs.readFileSync(sourcePath, "utf8"),
    {
      compilerOptions: {
        module: typescript.ModuleKind.CommonJS,
        target: typescript.ScriptTarget.ES2022,
      },
      fileName: sourcePath,
      reportDiagnostics: true,
    },
  );
  const errors = (transpiled.diagnostics || []).filter(
    (diagnostic) => diagnostic.category === typescript.DiagnosticCategory.Error,
  );
  assert.deepEqual(errors, [], "browser time transpilation failed");
  const module = { exports: {} };
  new Function("exports", "module", "require", transpiled.outputText)(
    module.exports,
    module,
    dashboardRequire,
  );
  return module.exports;
}

function localTimestampContracts(time) {
  const value = "2026-07-13T12:34:56Z";
  assert.equal(
    time.formatBrowserLocalTimestamp(value),
    new Date(value).toLocaleString(),
    "absolute request times must use the browser locale and time zone",
  );
  assert.equal(time.formatBrowserLocalTimestamp(null), "--");
  assert.equal(time.formatBrowserLocalTimestamp("not-a-date"), "not-a-date");

  assert.equal(
    time.formatBrowserTimelineTick(value),
    new Intl.DateTimeFormat(undefined, {
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    }).format(new Date(value)),
    "timeline labels must use the browser locale and time zone",
  );
}

function relativeTimestampContracts(time) {
  const now = Date.parse("2026-07-20T12:34:56Z");
  const value = "2026-07-13T12:34:56Z";
  assert.equal(
    time.formatBrowserRelativeTimestamp(value, now),
    new Intl.RelativeTimeFormat(undefined, { numeric: "auto" }).format(
      -7,
      "day",
    ),
    "request rows must match the relative-time treatment used by Memories",
  );
  assert.equal(
    time.formatBrowserRelativeTimestamp("not-a-date", now),
    "not-a-date",
  );
}

function main() {
  if (process.argv.length !== 3) {
    throw new Error("usage: test-browser-time.cjs <dashboard-dir>");
  }
  const time = loadBrowserTime(path.resolve(process.argv[2]));
  localTimestampContracts(time);
  relativeTimestampContracts(time);
  console.log("browser time harness: 2 contract groups passed");
}

main();
