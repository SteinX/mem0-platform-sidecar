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

async function testSupersededRotationCannotOverwriteNewerCookie(
  createCoordinator,
) {
  let now = 0;
  let sequence = 0;
  const coordinator = createCoordinator({
    now: () => now,
    sessionCacheTtlMs: 100,
    refreshUpstream: async () => {
      sequence += 1;
      return Response.json({
        access_token: `access-${sequence}`,
        refresh_token: `refresh-${sequence}`,
      });
    },
  });

  const firstRotation = await coordinator.refresh("refresh-0");
  assert.equal(
    coordinator.shouldSetRefreshCookie("refresh-0", firstRotation),
    true,
    "a live rotation must publish its successor cookie",
  );

  const cached = await coordinator.refresh("refresh-1");
  assert.equal(
    coordinator.shouldSetRefreshCookie("refresh-1", cached),
    false,
    "a current-token cache hit must not re-emit the same cookie",
  );

  now = 101;
  const secondRotation = await coordinator.refresh("refresh-1");
  assert.equal(
    coordinator.shouldSetRefreshCookie("refresh-1", secondRotation),
    true,
    "the next live rotation must publish its successor cookie",
  );
  assert.equal(
    coordinator.shouldSetRefreshCookie("refresh-0", firstRotation),
    false,
    "a delayed participant in an older rotation must not overwrite the newer cookie",
  );
}

async function testLogoutInvalidatesInflightRotation(createCoordinator) {
  let releaseUpstream;
  const upstreamGate = new Promise((resolve) => {
    releaseUpstream = resolve;
  });
  const coordinator = createCoordinator({
    refreshUpstream: async () => {
      await upstreamGate;
      return Response.json({
        access_token: "late-access",
        refresh_token: "late-refresh",
      });
    },
  });

  const pending = coordinator.refresh("logout-token");
  await new Promise((resolve) => setImmediate(resolve));
  coordinator.invalidateRefreshToken("logout-token");
  releaseUpstream();

  const result = await pending;
  assert.deepEqual(result, { status: "unauthorized" });
  assert.equal(
    coordinator.shouldSetRefreshCookie("logout-token", result),
    false,
    "an in-flight rotation must not restore a cookie after logout",
  );
}

async function testLogoutInvalidatesCompletedSlowResponse(createCoordinator) {
  const coordinator = createCoordinator({
    refreshUpstream: async () =>
      Response.json({
        access_token: "slow-access",
        refresh_token: "slow-refresh",
      }),
  });

  const predecessorResult = await coordinator.refresh("predecessor-token");
  coordinator.invalidateRefreshToken("predecessor-token");
  assert.equal(
    coordinator.shouldSetRefreshCookie("predecessor-token", predecessorResult),
    false,
    "a completed slow response must not restore its successor after logout",
  );

  const currentCoordinator = createCoordinator({
    refreshUpstream: async () =>
      Response.json({
        access_token: "current-access",
        refresh_token: "current-refresh",
      }),
  });
  const currentResult = await currentCoordinator.refresh("current-old");
  currentCoordinator.invalidateRefreshToken("current-refresh");
  assert.equal(
    currentCoordinator.shouldSetRefreshCookie("current-old", currentResult),
    false,
    "invalidating the current token must also suppress its pending cookie",
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

async function testAmbiguousFailureQuarantinesTheToken(createCoordinator) {
  let now = 0;
  let upstreamCalls = 0;
  const coordinator = createCoordinator({
    now: () => now,
    ambiguousTokenGraceMs: 100,
    refreshUpstream: async () => {
      upstreamCalls += 1;
      if (upstreamCalls === 1) {
        throw new Error("response lost after a possibly committed rotation");
      }
      if (upstreamCalls === 2) {
        return new Response(null, { status: 401 });
      }
      return Response.json({
        access_token: "recovered-access",
        refresh_token: "recovered-refresh",
      });
    },
  });

  assert.deepEqual(await coordinator.refresh("uncertain-token"), {
    status: "unavailable",
  });
  assert.deepEqual(await coordinator.refresh("uncertain-token"), {
    status: "unavailable",
  });
  assert.equal(
    upstreamCalls,
    1,
    "an ambiguous token must not be retried immediately and misclassified as unauthorized",
  );

  now = 101;
  assert.deepEqual(await coordinator.refresh("uncertain-token"), {
    status: "unavailable",
  });
  assert.equal(upstreamCalls, 2);

  now = 202;
  assert.deepEqual(await coordinator.refresh("uncertain-token"), {
    status: "authenticated",
    accessToken: "recovered-access",
    refreshToken: "recovered-refresh",
  });
  assert.equal(upstreamCalls, 3);
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
  assert.ok(
    coordinator.getStats().tokenGuards <= 4,
    "refresh-token invalidation guards must remain bounded",
  );
}

async function testAmbiguityHistoryIsBoundedWithoutPoisoningFresh401s(
  createCoordinator,
) {
  let failAmbiguously = true;
  const coordinator = createCoordinator({
    ambiguousTokenGraceMs: 0,
    maxCacheEntries: 2,
    refreshUpstream: async () => {
      if (failAmbiguously) throw new Error("response lost");
      return new Response(null, { status: 401 });
    },
  });

  await coordinator.refresh("uncertain-1");
  await coordinator.refresh("uncertain-2");
  await coordinator.refresh("uncertain-3");
  assert.ok(
    coordinator.getStats().ambiguousEntries <= 2,
    "ambiguous-token history must remain bounded",
  );

  failAmbiguously = false;
  assert.deepEqual(await coordinator.refresh("fresh-invalid-token"), {
    status: "unauthorized",
  });
}

async function testAmbiguityHistoryEventuallyExpires(createCoordinator) {
  let now = 0;
  let upstreamCalls = 0;
  const coordinator = createCoordinator({
    now: () => now,
    ambiguityHistoryTtlMs: 150,
    ambiguousTokenGraceMs: 100,
    refreshUpstream: async () => {
      upstreamCalls += 1;
      if (upstreamCalls === 1) throw new Error("response lost");
      return new Response(null, { status: 401 });
    },
  });

  assert.equal(
    (await coordinator.refresh("eventually-invalid")).status,
    "unavailable",
  );
  now = 101;
  assert.equal(
    (await coordinator.refresh("eventually-invalid")).status,
    "unavailable",
  );
  now = 151;
  assert.equal(
    (await coordinator.refresh("eventually-invalid")).status,
    "unauthorized",
  );
}

async function testHungRefreshTimesOutAndReleasesSingleflight(
  createCoordinator,
) {
  const coordinator = createCoordinator({
    refreshTimeoutMs: 10,
    refreshUpstream: async () => new Promise(() => {}),
  });

  const result = await coordinator.refresh("hung-token");
  assert.deepEqual(result, { status: "unavailable" });
  assert.equal(coordinator.getStats().inFlight, 0);
}

async function testBrowserRefreshClassification(
  requestDashboardSessionRefresh,
) {
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

function testClientUnauthorizedRetryStateMachine(dashboardSessionRetryAction) {
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
  await testSupersededRotationCannotOverwriteNewerCookie(createCoordinator);
  await testLogoutInvalidatesInflightRotation(createCoordinator);
  await testLogoutInvalidatesCompletedSlowResponse(createCoordinator);
  await testUpstreamFailuresAreClassifiedWithoutFalseLogout(createCoordinator);
  await testAmbiguousFailureQuarantinesTheToken(createCoordinator);
  await testExpiredEntriesAreSwept(createCoordinator);
  await testCacheIsBoundedAndEvictsOldEntries(createCoordinator);
  await testAmbiguityHistoryIsBoundedWithoutPoisoningFresh401s(
    createCoordinator,
  );
  await testAmbiguityHistoryEventuallyExpires(createCoordinator);
  await testHungRefreshTimesOutAndReleasesSingleflight(createCoordinator);
  await testBrowserRefreshClassification(
    clientModule.requestDashboardSessionRefresh,
  );
  testClientUnauthorizedRetryStateMachine(
    clientModule.dashboardSessionRetryAction,
  );
  testAxiosRetryMarkerWiring(clientModule.dashboardSessionRequestRetryAction);
  console.log("dashboard session refresh harness: 15 contracts passed");
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
