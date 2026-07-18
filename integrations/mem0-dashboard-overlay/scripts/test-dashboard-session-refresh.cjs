#!/usr/bin/env node
"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { createRequire } = require("node:module");

async function loadTypescriptModule(dashboardDir, modulePath) {
  const dashboardRequire = createRequire(
    path.join(dashboardDir, "package.json"),
  );
  const typescript = dashboardRequire("typescript");
  assert.ok(fs.existsSync(modulePath), `${modulePath} is missing`);
  const source = fs.readFileSync(modulePath, "utf8");
  const transpiled = typescript.transpileModule(source, {
    compilerOptions: {
      module: typescript.ModuleKind.ES2022,
      target: typescript.ScriptTarget.ES2022,
    },
    fileName: modulePath,
    reportDiagnostics: true,
  });
  const errors = (transpiled.diagnostics || []).filter(
    (diagnostic) => diagnostic.category === typescript.DiagnosticCategory.Error,
  );
  assert.deepEqual(errors, [], `${modulePath} transpilation failed`);

  const encoded = Buffer.from(transpiled.outputText).toString("base64");
  return import(`data:text/javascript;base64,${encoded}`);
}

async function testConcurrentRefreshesShareOneRotationAndCurrentTokenCache(
  createCoordinator,
) {
  let upstreamCalls = 0;
  let releaseUpstream;
  const upstreamGate = new Promise((resolve) => {
    releaseUpstream = resolve;
  });
  const coordinator = createCoordinator({
    refreshUpstream: async (refreshToken) => {
      upstreamCalls += 1;
      assert.equal(refreshToken, "old-refresh-token");
      await upstreamGate;
      return Response.json({
        access_token: "access-token",
        refresh_token: "rotated-refresh-token",
      });
    },
  });

  const first = coordinator.refresh("old-refresh-token");
  const second = coordinator.refresh("old-refresh-token");
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(
    upstreamCalls,
    1,
    "concurrent refreshes must share one rotation",
  );

  releaseUpstream();
  const [firstResult, secondResult] = await Promise.all([first, second]);
  assert.deepEqual(firstResult, {
    status: "authenticated",
    accessToken: "access-token",
    refreshToken: "rotated-refresh-token",
  });
  assert.deepEqual(secondResult, firstResult);

  const cached = await coordinator.refresh("rotated-refresh-token");
  assert.deepEqual(cached, firstResult);
  assert.equal(
    upstreamCalls,
    1,
    "the current rotated token must reuse its cached access token",
  );
}

async function testConsumedTokenReplayNeverReturnsSuccessorCredentials(
  createCoordinator,
) {
  let upstreamCalls = 0;
  const coordinator = createCoordinator({
    refreshUpstream: async () => {
      upstreamCalls += 1;
      return Response.json({
        access_token: "access-token",
        refresh_token: "rotated-refresh-token",
      });
    },
  });

  await coordinator.refresh("old-refresh-token");
  const replay = await coordinator.refresh("old-refresh-token");

  assert.deepEqual(replay, { status: "unavailable" });
  assert.equal(upstreamCalls, 1);
  assert.equal(
    "refreshToken" in replay,
    false,
    "a consumed token replay must never receive successor credentials",
  );
}

async function testUpstreamFailuresAreClassifiedWithoutFalseLogout(
  createCoordinator,
) {
  const cases = [
    ["401", async () => new Response(null, { status: 401 }), "unauthorized"],
    ["429", async () => new Response(null, { status: 429 }), "unavailable"],
    ["500", async () => new Response(null, { status: 500 }), "unavailable"],
    [
      "network",
      async () => {
        throw new Error("offline");
      },
      "unavailable",
    ],
    [
      "malformed success",
      async () => Response.json({ access_token: "missing-refresh" }),
      "unavailable",
    ],
  ];

  for (const [name, refreshUpstream, expectedStatus] of cases) {
    const coordinator = createCoordinator({ refreshUpstream });
    const result = await coordinator.refresh(`token-${name}`);
    assert.equal(result.status, expectedStatus, name);
  }
}

async function testExpiredEntriesAreSwept(createCoordinator) {
  let now = 0;
  let sequence = 0;
  const coordinator = createCoordinator({
    now: () => now,
    oldTokenGraceMs: 100,
    sessionCacheTtlMs: 200,
    refreshUpstream: async () => {
      sequence += 1;
      return Response.json({
        access_token: `access-${sequence}`,
        refresh_token: `rotated-${sequence}`,
      });
    },
  });

  await coordinator.refresh("old-1");
  assert.equal(coordinator.getStats().cachedEntries, 2);
  now = 201;
  await coordinator.refresh("old-2");
  assert.equal(
    coordinator.getStats().cachedEntries,
    2,
    "expired token keys and credentials must be swept globally",
  );
}

async function testCacheIsBoundedAndEvictsOldEntries(createCoordinator) {
  let sequence = 0;
  const coordinator = createCoordinator({
    maxCacheEntries: 2,
    refreshUpstream: async () => {
      sequence += 1;
      return Response.json({
        access_token: `access-${sequence}`,
        refresh_token: `rotated-${sequence}`,
      });
    },
  });

  await coordinator.refresh("old-1");
  await coordinator.refresh("old-2");
  assert.equal(coordinator.getStats().cachedEntries, 2);
  await coordinator.refresh("rotated-1");
  assert.equal(
    sequence,
    3,
    "the oldest cached session must be evicted when the cache is full",
  );
  assert.ok(coordinator.getStats().cachedEntries <= 2);
}

async function testHungRefreshTimesOutAndReleasesSingleflight(createCoordinator) {
  const coordinator = createCoordinator({
    refreshTimeoutMs: 10,
    refreshUpstream: async () => new Promise(() => {}),
  });

  const result = await coordinator.refresh("hung-token");
  assert.deepEqual(result, { status: "unavailable" });
  assert.equal(coordinator.getStats().inFlight, 0);
}

async function testBrowserRefreshClassification(requestDashboardSessionRefresh) {
  const cases = [
    [
      "success",
      async () => Response.json({ access_token: "access-token" }),
      { status: "authenticated", accessToken: "access-token" },
    ],
    [
      "401",
      async () => new Response(null, { status: 401 }),
      { status: "unauthorized" },
    ],
    [
      "503",
      async () => new Response(null, { status: 503 }),
      { status: "unavailable" },
    ],
    [
      "network",
      async () => {
        throw new Error("offline");
      },
      { status: "unavailable" },
    ],
    [
      "malformed success",
      async () => Response.json({}),
      { status: "unavailable" },
    ],
  ];

  for (const [name, fetchSession, expected] of cases) {
    const result = await requestDashboardSessionRefresh(fetchSession);
    assert.deepEqual(result, expected, name);
  }
}

function testClientUnauthorizedRetryStateMachine(
  dashboardSessionRetryAction,
) {
  assert.equal(dashboardSessionRetryAction(401, false), "refresh");
  assert.equal(dashboardSessionRetryAction(401, true), "logout");
  assert.equal(dashboardSessionRetryAction(500, false), "ignore");
}

function testAxiosRetryMarkerWiring(dashboardSessionRequestRetryAction) {
  assert.equal(
    dashboardSessionRequestRetryAction(401, {}),
    "refresh",
    "an unmarked first Axios 401 must refresh",
  );
  assert.equal(
    dashboardSessionRequestRetryAction(401, { __mem0AuthRetry: true }),
    "logout",
    "a marked second Axios 401 must logout",
  );
  assert.equal(
    dashboardSessionRequestRetryAction(401, undefined),
    "logout",
    "a 401 without a retryable request config cannot be retried",
  );
}

async function main() {
  if (process.argv.length < 3 || process.argv.length > 4) {
    throw new Error(
      "usage: test-dashboard-session-refresh.cjs <dashboard-dir> [source-root]",
    );
  }
  const dashboardDir = path.resolve(process.argv[2]);
  const sourceRoot = path.resolve(process.argv[3] || process.argv[2]);
  const serverModule = await loadTypescriptModule(
    dashboardDir,
    path.join(sourceRoot, "src/utils/dashboard-session-refresh.ts"),
  );
  const clientModule = await loadTypescriptModule(
    dashboardDir,
    path.join(sourceRoot, "src/utils/dashboard-session-client.ts"),
  );
  const createCoordinator =
    serverModule.createDashboardSessionRefreshCoordinator;

  await testConcurrentRefreshesShareOneRotationAndCurrentTokenCache(
    createCoordinator,
  );
  await testConsumedTokenReplayNeverReturnsSuccessorCredentials(
    createCoordinator,
  );
  await testUpstreamFailuresAreClassifiedWithoutFalseLogout(createCoordinator);
  await testExpiredEntriesAreSwept(createCoordinator);
  await testCacheIsBoundedAndEvictsOldEntries(createCoordinator);
  await testHungRefreshTimesOutAndReleasesSingleflight(createCoordinator);
  await testBrowserRefreshClassification(
    clientModule.requestDashboardSessionRefresh,
  );
  testClientUnauthorizedRetryStateMachine(
    clientModule.dashboardSessionRetryAction,
  );
  testAxiosRetryMarkerWiring(
    clientModule.dashboardSessionRequestRetryAction,
  );
  console.log("dashboard session refresh harness: 9 contracts passed");
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
