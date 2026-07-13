#!/usr/bin/env node
"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { createRequire } = require("node:module");

async function loadProxyModule(dashboardDir) {
  const dashboardRequire = createRequire(
    path.join(dashboardDir, "package.json"),
  );
  const typescript = dashboardRequire("typescript");
  const proxyPath = path.join(dashboardDir, "src/utils/sidecar-proxy.ts");
  const source = fs.readFileSync(proxyPath, "utf8");
  const transpiled = typescript.transpileModule(source, {
    compilerOptions: {
      module: typescript.ModuleKind.ES2022,
      target: typescript.ScriptTarget.ES2022,
    },
    fileName: proxyPath,
    reportDiagnostics: true,
  });
  const errors = (transpiled.diagnostics || []).filter(
    (diagnostic) => diagnostic.category === typescript.DiagnosticCategory.Error,
  );
  assert.deepEqual(errors, [], "sidecar proxy transpilation failed");

  const encoded = Buffer.from(transpiled.outputText).toString("base64");
  return import(`data:text/javascript;base64,${encoded}`);
}

function proxyOptions(fetchUpstream, overrides = {}) {
  return {
    baseUrl: "http://sidecar.internal",
    configuredProjectId: "runtime project",
    validateDashboardSession: async () => true,
    fetchUpstream,
    ...overrides,
  };
}

function traceProxyOptions(fetchUpstream, overrides = {}) {
  return proxyOptions(fetchUpstream, {
    configuredProjectId: "runtime-project",
    configuredAppId: "runtime-app",
    ...overrides,
  });
}

function jsonHeaders(extra = {}) {
  return { "Content-Type": "application/json", ...extra };
}

async function testEntityQueryForcesRuntimeScope(proxy) {
  const calls = [];
  const payload = {
    project_id: "forged-body-project",
    app_id: "forged-body-app",
    entity_type: "user",
    match: "all",
    filters: [{ field: "user_id", operator: "equals", value: "alice" }],
    date_range: { from: null, to: null },
    page: 2,
    page_size: 20,
  };
  const response = await proxy(
    new Request(
      "http://dashboard.local/api/sidecar/v1/entities/query?project_id=forged-query-project&app_id=forged-query-app&trace=forged",
      {
        method: "POST",
        headers: jsonHeaders({ "X-Request-ID": "entity-query-123" }),
        body: JSON.stringify(payload),
      },
    ),
    "/v1/entities/query",
    proxyOptions(
      async (url, init) => {
        calls.push({ url: url.toString(), init });
        return Response.json({ results: [], total: 0 });
      },
      { configuredAppId: "runtime-app" },
    ),
  );

  assert.equal(response.status, 200);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, "http://sidecar.internal/v1/entities/query");
  assert.equal(calls[0].init.method, "POST");
  assert.equal(calls[0].init.headers.get("X-Request-ID"), "entity-query-123");
  assert.deepEqual(JSON.parse(calls[0].init.body), {
    entity_type: payload.entity_type,
    match: payload.match,
    filters: payload.filters,
    date_range: payload.date_range,
    page: payload.page,
    page_size: payload.page_size,
    project_id: "runtime project",
    app_id: "runtime-app",
  });
}

async function testEntityItemsEncodeTypeAndIdAndForceRuntimeScope(proxy) {
  const scopes = [
    [undefined, "project_id=runtime+project"],
    ["runtime-app", "project_id=runtime+project&app_id=runtime-app"],
  ];
  for (const [configuredAppId, expectedScope] of scopes) {
    for (const method of ["GET", "DELETE"]) {
      const label = `${method} ${configuredAppId ?? "no configured app"}`;
      const calls = [];
      const response = await proxy(
        new Request(
          "http://dashboard.local/api/sidecar/v1/entities/%75ser/team%2Falice%252Farchive?project_id=forged&app_id=forged-app&trace=forged",
          { method },
        ),
        "/v1/entities/user/team/alice%2Farchive",
        proxyOptions(
          async (url, init) => {
            calls.push({ url: url.toString(), init });
            return Response.json(
              method === "GET"
                ? { type: "user", entity_id: "team/alice%2Farchive" }
                : { status: "SUCCEEDED" },
            );
          },
          { configuredAppId },
        ),
      );

      assert.equal(response.status, 200, label);
      assert.equal(calls.length, 1, label);
      assert.equal(
        calls[0].url,
        `http://sidecar.internal/v1/entities/user/team%2Falice%252Farchive?${expectedScope}`,
        label,
      );
      assert.equal(calls[0].init.method, method);
      assert.equal(calls[0].init.body, undefined);
    }
  }
}

async function testEntityRoutesRejectRebuildAndUnsafeItemPaths(proxy) {
  const rejected = [
    ["POST", "/v1/projects/runtime-project/entities/rebuild"],
    ["GET", "/v1/entities/%2E%2E/alice"],
    ["DELETE", "/v1/entities/safe%2F..%2Fsession/alice"],
    ["GET", "/v1/entities/user/alice/extra"],
    ["PATCH", "/v1/entities/user/alice"],
    ["DELETE", "/v1/entities/query"],
  ];

  for (const [method, normalizedPath] of rejected) {
    let fetchCalled = false;
    const response = await proxy(
      new Request("http://dashboard.local/api/sidecar/rejected", {
        method,
        headers: method === "POST" ? jsonHeaders() : undefined,
        body: method === "POST" ? "{}" : undefined,
      }),
      normalizedPath,
      proxyOptions(async () => {
        fetchCalled = true;
        return Response.json({});
      }),
    );

    assert.equal(response.status, 403, `${method} ${normalizedPath}`);
    assert.equal(fetchCalled, false, `${method} ${normalizedPath}`);
  }
}

async function testUnsupportedSafeEntityTypePassesBackendValidation(proxy) {
  const calls = [];
  const response = await proxy(
    new Request(
      "http://dashboard.local/api/sidecar/v1/entities/session/session-one?project_id=forged",
      { method: "GET" },
    ),
    "/v1/entities/session/session-one",
    proxyOptions(async (url, init) => {
      calls.push({ url: url.toString(), init });
      return Response.json(
        { detail: "Unsupported entity type" },
        { status: 422 },
      );
    }),
  );

  assert.equal(response.status, 422);
  assert.deepEqual(await response.json(), {
    detail: "Unsupported entity type",
  });
  assert.equal(calls.length, 1);
  assert.equal(
    calls[0].url,
    "http://sidecar.internal/v1/entities/session/session-one?project_id=runtime+project",
  );
}

async function testUnauthenticatedEntityRequestsAreRejected(proxy) {
  for (const [method, path, body] of [
    ["POST", "/v1/entities/query", "{}"],
    ["GET", "/v1/entities/user/alice", undefined],
    ["DELETE", "/v1/entities/user/alice", undefined],
  ]) {
    let fetchCalled = false;
    const response = await proxy(
      new Request(`http://dashboard.local/api/sidecar${path}`, {
        method,
        headers: body === undefined ? undefined : jsonHeaders(),
        body,
      }),
      path,
      proxyOptions(
        async () => {
          fetchCalled = true;
          return Response.json({});
        },
        { validateDashboardSession: async () => false },
      ),
    );

    assert.equal(response.status, 401, `${method} ${path}`);
    assert.deepEqual(await response.json(), { error: "Unauthorized" });
    assert.equal(fetchCalled, false, `${method} ${path}`);
  }
}

async function testEventQueryForcesRuntimeScope(proxy) {
  const calls = [];
  const payload = {
    project_id: "forged-body-project",
    app_id: "forged-body-app",
    operation: "SEARCH",
    statuses: ["SUCCEEDED"],
    has_results: true,
    date_range: { from: null, to: null },
    entity_filters: { user_id: "alice" },
    page: 2,
    page_size: 20,
  };
  const response = await proxy(
    new Request(
      "http://dashboard.local/api/sidecar/v1/events/query?project_id=forged-query-project&app_id=forged-query-app",
      {
        method: "POST",
        headers: jsonHeaders({ "X-Request-ID": "event-query-123" }),
        body: JSON.stringify(payload),
      },
    ),
    "/v1/events/query",
    traceProxyOptions(async (url, init) => {
      calls.push({ url: url.toString(), init });
      return Response.json({ results: [], total: 0 });
    }),
  );

  assert.equal(response.status, 200);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, "http://sidecar.internal/v1/events/query");
  assert.equal(calls[0].init.method, "POST");
  assert.equal(calls[0].init.headers.get("X-Request-ID"), "event-query-123");
  assert.deepEqual(JSON.parse(calls[0].init.body), {
    operation: payload.operation,
    statuses: payload.statuses,
    has_results: payload.has_results,
    date_range: payload.date_range,
    entity_filters: payload.entity_filters,
    page: payload.page,
    page_size: payload.page_size,
    project_id: "runtime-project",
    app_id: "runtime-app",
  });
}

async function testEventDetailEncodesIdAndForcesRuntimeScope(proxy) {
  const calls = [];
  const response = await proxy(
    new Request(
      "http://dashboard.local/api/sidecar/v1/event/event%2Fone?project_id=forged&app_id=forged-app&trace=forged",
      { method: "GET" },
    ),
    "/v1/event/event/one",
    traceProxyOptions(async (url, init) => {
      calls.push({ url: url.toString(), init });
      return Response.json({ id: "event/one" });
    }),
  );

  assert.equal(response.status, 200);
  assert.equal(calls.length, 1);
  assert.equal(
    calls[0].url,
    "http://sidecar.internal/v1/event/event%252Fone?project_id=runtime-project&app_id=runtime-app",
  );
  assert.equal(calls[0].init.body, undefined);
}

async function testEventRoutesRejectMutationsAndNearMatches(proxy) {
  const rejected = [
    ["GET", "/v1/events/query"],
    ["PUT", "/v1/events/query"],
    ["PATCH", "/v1/events/query"],
    ["DELETE", "/v1/events/query"],
    ["POST", "/v1/event/event-one"],
    ["PUT", "/v1/event/event-one"],
    ["PATCH", "/v1/event/event-one"],
    ["DELETE", "/v1/event/event-one"],
    ["GET", "/v1/events"],
    ["GET", "/v1/events/event-one"],
    ["GET", "/v1/event/event-one/extra"],
  ];

  for (const [method, normalizedPath] of rejected) {
    let fetchCalled = false;
    const response = await proxy(
      new Request(`http://dashboard.local/api/sidecar${normalizedPath}`, {
        method,
        body: ["POST", "PUT", "PATCH"].includes(method) ? "{}" : undefined,
      }),
      normalizedPath,
      traceProxyOptions(async () => {
        fetchCalled = true;
        return Response.json({});
      }),
    );
    assert.equal(response.status, 403, `${method} ${normalizedPath}`);
    assert.equal(fetchCalled, false, `${method} ${normalizedPath}`);
  }
}

async function testEventDetailRejectsUnsafeEncodedIds(proxy) {
  const rejected = [
    "/v1/event/..",
    "/v1/event/%2E%2E",
    "/v1/event/%2e%2e%2fhealth",
    "/v1/event/safe%2f..%2fhealth",
    "/v1/event/%2e%2e%5chealth",
    "/v1/event/safe%5c..%5chealth",
    "/v1/event/%71uery",
    "/v1/event/event%252Fone",
    "/v1/event/event%00one",
    "/v1/event/event%1fone",
    "/v1/event/event%7fone",
    "/v1/event/event%",
    "/v1/event/event%GGone",
    "/v1/event/event%FFone",
  ];

  for (const normalizedPath of rejected) {
    let fetchCalled = false;
    const response = await proxy(
      new Request(`http://dashboard.local/api/sidecar${normalizedPath}`, {
        method: "GET",
      }),
      normalizedPath,
      traceProxyOptions(async () => {
        fetchCalled = true;
        return Response.json({});
      }),
    );
    assert.equal(response.status, 403, normalizedPath);
    assert.equal(fetchCalled, false, normalizedPath);
  }
}

async function testEventRoutesFailClosedWithoutPortableRuntimeScope(proxy) {
  const invalidScopes = [
    { configuredAppId: "" },
    { configuredAppId: "two words" },
    { configuredAppId: "\u0000app" },
    { configuredAppId: "a".repeat(257) },
    { configuredProjectId: "" },
    { configuredProjectId: "two words" },
    { configuredProjectId: "p".repeat(129) },
  ];

  for (const scope of invalidScopes) {
    let fetchCalled = false;
    const response = await proxy(
      new Request("http://dashboard.local/api/sidecar/v1/events/query", {
        method: "POST",
        headers: jsonHeaders(),
        body: "{}",
      }),
      "/v1/events/query",
      traceProxyOptions(async () => {
        fetchCalled = true;
        return Response.json({ results: [] });
      }, scope),
    );
    assert.equal(response.status, 500, JSON.stringify(scope));
    assert.deepEqual(await response.json(), {
      error: "Sidecar trace scope is not configured",
    });
    assert.equal(fetchCalled, false, JSON.stringify(scope));
  }
}

async function testEventRoutesAllowServerDefaultAppResolution(proxy) {
  const calls = [];
  const options = proxyOptions(async (url, init) => {
    calls.push({ url: url.toString(), init });
    return Response.json({ results: [] });
  }, { configuredProjectId: "runtime-project", configuredAppId: undefined });

  const queryResponse = await proxy(
    new Request("http://dashboard.local/api/sidecar/v1/events/query", {
      method: "POST",
      headers: jsonHeaders(),
      body: JSON.stringify({ app_id: "forged-app", page: 1 }),
    }),
    "/v1/events/query",
    options,
  );
  const detailResponse = await proxy(
    new Request(
      "http://dashboard.local/api/sidecar/v1/event/event%2Fone?app_id=forged-app",
      { method: "GET" },
    ),
    "/v1/event/event/one",
    options,
  );

  assert.equal(queryResponse.status, 200);
  assert.equal(detailResponse.status, 200);
  assert.deepEqual(JSON.parse(calls[0].init.body), {
    page: 1,
    project_id: "runtime-project",
  });
  assert.equal(
    calls[1].url,
    "http://sidecar.internal/v1/event/event%252Fone?project_id=runtime-project",
  );
}

async function testUnauthenticatedEventRequestsAreRejected(proxy) {
  for (const [method, path, body] of [
    ["POST", "/v1/events/query", "{}"],
    ["GET", "/v1/event/event-one", undefined],
  ]) {
    let fetchCalled = false;
    const response = await proxy(
      new Request(`http://dashboard.local/api/sidecar${path}`, {
        method,
        headers: body === undefined ? undefined : jsonHeaders(),
        body,
      }),
      path,
      traceProxyOptions(
        async () => {
          fetchCalled = true;
          return Response.json({});
        },
        { validateDashboardSession: async () => false },
      ),
    );
    assert.equal(response.status, 401, `${method} ${path}`);
    assert.deepEqual(await response.json(), { error: "Unauthorized" });
    assert.equal(fetchCalled, false, `${method} ${path}`);
  }
}

async function testUnauthenticatedAllowedRouteHidesMissingBaseUrl(proxy) {
  let fetchCalled = false;
  const response = await proxy(
    new Request("http://dashboard.local/api/sidecar/v1/events/query", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    }),
    "/v1/events/query",
    traceProxyOptions(
      async () => {
        fetchCalled = true;
        return Response.json({});
      },
      {
        baseUrl: null,
        validateDashboardSession: async () => false,
      },
    ),
  );

  assert.equal(response.status, 401);
  assert.deepEqual(await response.json(), { error: "Unauthorized" });
  assert.equal(fetchCalled, false);
}

async function testUnauthenticatedRouteDoesNotInspectUnsafeProjectConfig(
  proxy,
) {
  let fetchCalled = false;
  const response = await proxy(
    new Request(
      "http://dashboard.local/api/sidecar/v1/projects/forged/categories",
      { method: "GET" },
    ),
    "/v1/projects/forged/categories",
    proxyOptions(
      async () => {
        fetchCalled = true;
        return Response.json({});
      },
      {
        baseUrl: null,
        configuredProjectId: "\ud800",
        validateDashboardSession: async () => false,
      },
    ),
  );

  assert.equal(response.status, 401);
  assert.deepEqual(await response.json(), { error: "Unauthorized" });
  assert.equal(fetchCalled, false);
}

async function testUnsupportedRouteWinsBeforeAuthAndConfiguration(proxy) {
  let sessionValidated = false;
  let fetchCalled = false;
  const response = await proxy(
    new Request("http://dashboard.local/api/sidecar/v1/events", {
      method: "GET",
    }),
    "/v1/events",
    traceProxyOptions(
      async () => {
        fetchCalled = true;
        return Response.json({});
      },
      {
        baseUrl: null,
        validateDashboardSession: async () => {
          sessionValidated = true;
          return false;
        },
      },
    ),
  );

  assert.equal(response.status, 403);
  assert.deepEqual(await response.json(), {
    error: "Sidecar route is not allowed",
  });
  assert.equal(sessionValidated, false);
  assert.equal(fetchCalled, false);
}

async function testScopedJsonRoutesRequireJsonMediaType(proxy) {
  const requests = [
    ["POST", "/v1/entities/query"],
    ["POST", "/v1/events/query"],
    ["POST", "/v1/memories/query"],
    ["PATCH", "/v1/memories/memory-one"],
    ["POST", "/v1/exports"],
  ];
  for (const [method, path] of requests) {
    let fetchCalled = false;
    const response = await proxy(
      new Request(`http://dashboard.local/api/sidecar${path}`, {
        method,
        headers: { "Content-Type": "text/plain" },
        body: "{}",
      }),
      path,
      traceProxyOptions(async () => {
        fetchCalled = true;
        return Response.json({});
      }),
    );

    assert.equal(response.status, 415, `${method} ${path}`);
    assert.deepEqual(await response.json(), {
      error: "Content-Type must be application/json",
    });
    assert.equal(fetchCalled, false, `${method} ${path}`);
  }
}

async function testEventQueryRejectsInvalidJsonAndArrayBodies(proxy) {
  for (const body of ["{not-json", "[]"]) {
    let fetchCalled = false;
    const response = await proxy(
      new Request("http://dashboard.local/api/sidecar/v1/events/query", {
        method: "POST",
        headers: jsonHeaders(),
        body,
      }),
      "/v1/events/query",
      traceProxyOptions(async () => {
        fetchCalled = true;
        return Response.json({});
      }),
    );

    assert.equal(response.status, 400, body);
    assert.deepEqual(await response.json(), { error: "Invalid JSON body" });
    assert.equal(fetchCalled, false, body);
  }
}

async function testAuthDisabledEventQueryRejectsDeclaredOversizedJson(proxy) {
  let fetchCalled = false;
  const response = await proxy(
    new Request("http://dashboard.local/api/sidecar/v1/events/query", {
      method: "POST",
      headers: jsonHeaders({ "Content-Length": "65537" }),
      body: "{}",
    }),
    "/v1/events/query",
    traceProxyOptions(
      async () => {
        fetchCalled = true;
        return Response.json({});
      },
      { validateDashboardSession: async () => true },
    ),
  );

  assert.equal(response.status, 413);
  assert.deepEqual(await response.json(), { error: "JSON body is too large" });
  assert.equal(fetchCalled, false);
}

async function testAuthDisabledEventQueryRejectsStreamedOversizedJson(proxy) {
  let fetchCalled = false;
  const body = new ReadableStream({
    start(controller) {
      controller.enqueue(new Uint8Array(40_000));
      controller.enqueue(new Uint8Array(40_000));
    },
    cancel() {
      throw new Error("hostile stream refused cancellation");
    },
  });
  const response = await proxy(
    new Request("http://dashboard.local/api/sidecar/v1/events/query", {
      method: "POST",
      headers: jsonHeaders(),
      body,
      duplex: "half",
    }),
    "/v1/events/query",
    traceProxyOptions(
      async () => {
        fetchCalled = true;
        return Response.json({});
      },
      { validateDashboardSession: async () => true },
    ),
  );

  assert.equal(response.status, 413);
  assert.deepEqual(await response.json(), { error: "JSON body is too large" });
  assert.equal(fetchCalled, false);
}

async function testEventQueryRejectsInvalidUtf8Json(proxy) {
  let fetchCalled = false;
  const body = new ReadableStream({
    start(controller) {
      controller.enqueue(new Uint8Array([0xff]));
      controller.close();
    },
  });
  const response = await proxy(
    new Request("http://dashboard.local/api/sidecar/v1/events/query", {
      method: "POST",
      headers: jsonHeaders(),
      body,
      duplex: "half",
    }),
    "/v1/events/query",
    traceProxyOptions(async () => {
      fetchCalled = true;
      return Response.json({});
    }),
  );

  assert.equal(response.status, 400);
  assert.deepEqual(await response.json(), { error: "Invalid JSON body" });
  assert.equal(fetchCalled, false);
}

async function testMemoryQueryForcesRuntimeScopeAndPreservesQuery(proxy) {
  const calls = [];
  const payload = {
    project_id: "forged-body-project",
    app_id: "forged-body-app",
    match: "all",
    filters: [{ field: "user_id", operator: "equals", value: "alice" }],
    date_range: { from: null, to: null },
    page: 2,
    page_size: 20,
    sort: "created_at_desc",
  };
  const response = await proxy(
    new Request(
      "http://dashboard.local/api/sidecar/v1/memories/query?project_id=forged-query-project&app_id=forged-query-app&trace=query",
      {
        method: "POST",
        headers: jsonHeaders({ "X-Request-ID": "memory-query-123" }),
        body: JSON.stringify(payload),
      },
    ),
    "/v1/memories/query",
    proxyOptions(async (url, init) => {
      calls.push({ url: url.toString(), init });
      return Response.json({ results: [], total: 0 });
    }),
  );

  assert.equal(response.status, 200);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, "http://sidecar.internal/v1/memories/query");
  assert.equal(calls[0].init.method, "POST");
  assert.equal(calls[0].init.headers.get("X-Request-ID"), "memory-query-123");
  assert.deepEqual(JSON.parse(calls[0].init.body), {
    match: payload.match,
    filters: payload.filters,
    date_range: payload.date_range,
    page: payload.page,
    page_size: payload.page_size,
    sort: payload.sort,
    project_id: "runtime project",
  });
}

async function testMemoryQueryUsesOnlyConfiguredAppScope(proxy) {
  const calls = [];
  const response = await proxy(
    new Request(
      "http://dashboard.local/api/sidecar/v1/memories/query?app_id=forged-query-app",
      {
        method: "POST",
        headers: jsonHeaders(),
        body: JSON.stringify({ app_id: "forged-body-app", match: "all" }),
      },
    ),
    "/v1/memories/query",
    proxyOptions(
      async (url, init) => {
        calls.push({ url: url.toString(), init });
        return Response.json({ results: [] });
      },
      { configuredAppId: "app-y" },
    ),
  );

  assert.equal(response.status, 200);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, "http://sidecar.internal/v1/memories/query");
  assert.deepEqual(JSON.parse(calls[0].init.body), {
    match: "all",
    project_id: "runtime project",
    app_id: "app-y",
  });
}

async function testMemoryQueryRejectsInvalidJson(proxy) {
  let fetchCalled = false;
  const response = await proxy(
    new Request("http://dashboard.local/api/sidecar/v1/memories/query", {
      method: "POST",
      headers: jsonHeaders(),
      body: "{not-json",
    }),
    "/v1/memories/query",
    proxyOptions(async () => {
      fetchCalled = true;
      return Response.json({ results: [] });
    }),
  );

  assert.equal(response.status, 400);
  assert.deepEqual(await response.json(), { error: "Invalid JSON body" });
  assert.equal(fetchCalled, false);
}

async function testMemoryQueryRejectsArrayBody(proxy) {
  let fetchCalled = false;
  const response = await proxy(
    new Request("http://dashboard.local/api/sidecar/v1/memories/query", {
      method: "POST",
      headers: jsonHeaders(),
      body: "[]",
    }),
    "/v1/memories/query",
    proxyOptions(async () => {
      fetchCalled = true;
      return Response.json({ results: [] });
    }),
  );

  assert.equal(response.status, 400);
  assert.deepEqual(await response.json(), { error: "Invalid JSON body" });
  assert.equal(fetchCalled, false);
}

async function testMemoryDetailEncodesIdAndForcesRuntimeScope(proxy) {
  const calls = [];
  const response = await proxy(
    new Request(
      "http://dashboard.local/api/sidecar/v1/memories/memory%2Fone?project_id=forged&app_id=forged-app&trace=detail",
      { method: "GET" },
    ),
    "/v1/memories/memory/one",
    proxyOptions(async (url, init) => {
      calls.push({ url: url.toString(), init });
      return Response.json({ id: "memory/one" });
    }),
  );

  assert.equal(response.status, 200);
  assert.equal(calls.length, 1);
  assert.equal(
    calls[0].url,
    "http://sidecar.internal/v1/memories/memory%252Fone?project_id=runtime+project",
  );
  assert.equal(calls[0].init.body, undefined);
}

async function testMemoryHistoryKeepsExactSuffixAndEncodedId(proxy) {
  const calls = [];
  const response = await proxy(
    new Request(
      "http://dashboard.local/api/sidecar/v1/memories/memory%20one/history?project_id=forged&app_id=forged-app",
      { method: "GET" },
    ),
    "/v1/memories/memory%20one/history",
    proxyOptions(async (url, init) => {
      calls.push({ url: url.toString(), init });
      return Response.json({ results: [] });
    }),
  );

  assert.equal(response.status, 200);
  assert.equal(calls.length, 1);
  assert.equal(
    calls[0].url,
    "http://sidecar.internal/v1/memories/memory%2520one/history?project_id=runtime+project",
  );
}

async function testMemoryHistoryRecoversEncodedSlashFromRequestUrl(proxy) {
  const calls = [];
  const response = await proxy(
    new Request(
      "http://dashboard.local/api/sidecar/v1/memories/memory%2Fone/history",
      { method: "GET" },
    ),
    "/v1/memories/memory/one/history",
    proxyOptions(async (url, init) => {
      calls.push({ url: url.toString(), init });
      return Response.json({ results: [] });
    }),
  );

  assert.equal(response.status, 200);
  assert.equal(calls.length, 1);
  assert.equal(
    calls[0].url,
    "http://sidecar.internal/v1/memories/memory%252Fone/history?project_id=runtime+project",
  );
}

async function testMemoryPatchPreservesFieldsAndForcesRuntimeScope(proxy) {
  const calls = [];
  const payload = {
    text: "updated",
    metadata: { source: "dashboard" },
    expiration_date: "2027-01-01T00:00:00Z",
    project_id: "forged-project",
    app_id: "forged-app",
  };
  const response = await proxy(
    new Request(
      "http://dashboard.local/api/sidecar/v1/memories/memory-one?project_id=forged-query&app_id=forged-query-app",
      {
        method: "PATCH",
        headers: jsonHeaders(),
        body: JSON.stringify(payload),
      },
    ),
    "/v1/memories/memory-one",
    proxyOptions(
      async (url, init) => {
        calls.push({ url: url.toString(), init });
        return Response.json({ memory: { id: "memory-one" } });
      },
      { configuredAppId: "app-y" },
    ),
  );

  assert.equal(response.status, 200);
  assert.equal(calls.length, 1);
  assert.equal(
    calls[0].url,
    "http://sidecar.internal/v1/memories/memory-one?project_id=runtime+project&app_id=app-y",
  );
  assert.deepEqual(JSON.parse(calls[0].init.body), {
    text: "updated",
    metadata: { source: "dashboard" },
    expiration_date: "2027-01-01T00:00:00Z",
    project_id: "runtime project",
    app_id: "app-y",
  });
}

async function testMemoryPatchRejectsArrayBody(proxy) {
  let fetchCalled = false;
  const response = await proxy(
    new Request("http://dashboard.local/api/sidecar/v1/memories/memory-one", {
      method: "PATCH",
      headers: jsonHeaders(),
      body: "[]",
    }),
    "/v1/memories/memory-one",
    proxyOptions(async () => {
      fetchCalled = true;
      return Response.json({});
    }),
  );

  assert.equal(response.status, 400);
  assert.deepEqual(await response.json(), { error: "Invalid JSON body" });
  assert.equal(fetchCalled, false);
}

async function testMemoryPatchRejectsInvalidJson(proxy) {
  let fetchCalled = false;
  const response = await proxy(
    new Request("http://dashboard.local/api/sidecar/v1/memories/memory-one", {
      method: "PATCH",
      headers: jsonHeaders(),
      body: "{not-json",
    }),
    "/v1/memories/memory-one",
    proxyOptions(async () => {
      fetchCalled = true;
      return Response.json({});
    }),
  );

  assert.equal(response.status, 400);
  assert.deepEqual(await response.json(), { error: "Invalid JSON body" });
  assert.equal(fetchCalled, false);
}

async function testMemoryDeleteForcesRuntimeScope(proxy) {
  const calls = [];
  const response = await proxy(
    new Request(
      "http://dashboard.local/api/sidecar/v1/memories/memory-one?project_id=forged&app_id=forged-app",
      { method: "DELETE" },
    ),
    "/v1/memories/memory-one",
    proxyOptions(async (url, init) => {
      calls.push({ url: url.toString(), init });
      return new Response(null, { status: 204 });
    }),
  );

  assert.equal(response.status, 204);
  assert.equal(calls.length, 1);
  assert.equal(
    calls[0].url,
    "http://sidecar.internal/v1/memories/memory-one?project_id=runtime+project",
  );
}

async function testMemoryRoutesRejectTraversalDoubleEncodingAndExtraSegments(
  proxy,
) {
  const rejected = [
    ["GET", "/v1/memories/.."],
    ["GET", "/v1/memories/%2E%2E"],
    ["GET", "/v1/memories/%2e%2e%2fhealth"],
    ["GET", "/v1/memories/safe%2f..%2fhealth"],
    ["GET", "/v1/memories/%2e%2e%5chealth"],
    ["GET", "/v1/memories/safe%5c..%5chealth"],
    ["GET", "/v1/memories/%71uery"],
    ["GET", "/v1/memories/%71uery/history"],
    ["GET", "/v1/memories/safe%00name"],
    ["GET", "/v1/memories/safe%1fname"],
    ["GET", "/v1/memories/safe%7fname"],
    ["GET", "/v1/memories/memory%252Fone"],
    ["GET", "/v1/memories/%2571uery"],
    ["GET", "/v1/memories/memory-one/history/extra"],
    ["GET", "/v1/memories/memory-one/extra"],
    ["GET", "/v1/memories/query"],
    ["PATCH", "/v1/memories/query"],
    ["DELETE", "/v1/memories/query"],
    ["POST", "/v1/memories/memory-one"],
    ["PATCH", "/v1/memories/memory-one/history"],
    ["DELETE", "/v1/memories/memory-one/history"],
  ];

  for (const [method, normalizedPath] of rejected) {
    let fetchCalled = false;
    const response = await proxy(
      new Request(`http://dashboard.local/api/sidecar${normalizedPath}`, {
        method,
        body: method === "PATCH" || method === "POST" ? "{}" : undefined,
      }),
      normalizedPath,
      proxyOptions(async () => {
        fetchCalled = true;
        return Response.json({});
      }),
    );
    assert.equal(response.status, 403, `${method} ${normalizedPath}`);
    assert.equal(fetchCalled, false, `${method} ${normalizedPath}`);
  }
}

async function testUnauthenticatedMemoryRequestIsRejected(proxy) {
  let fetchCalled = false;
  const options = proxyOptions(async () => {
    fetchCalled = true;
    return Response.json({ results: [] });
  });
  options.validateDashboardSession = async () => false;
  const response = await proxy(
    new Request("http://dashboard.local/api/sidecar/v1/memories/query", {
      method: "POST",
      headers: jsonHeaders(),
      body: "{}",
    }),
    "/v1/memories/query",
    options,
  );

  assert.equal(response.status, 401);
  assert.deepEqual(await response.json(), { error: "Unauthorized" });
  assert.equal(fetchCalled, false);
}

async function testUpstreamFailureDoesNotLeakInternalDetails(proxy) {
  const response = await proxy(
    new Request("http://dashboard.local/api/sidecar/v1/memories/memory-one", {
      method: "GET",
    }),
    "/v1/memories/memory-one",
    proxyOptions(async () => {
      throw new Error(
        "timeout http://user:secret@sidecar.internal/v1/memories/memory-one",
      );
    }),
  );

  assert.equal(response.status, 502);
  const body = await response.text();
  assert.equal(body.includes("sidecar.internal"), false);
  assert.equal(body.includes("secret"), false);
  assert.deepEqual(JSON.parse(body), {
    error: "Sidecar upstream request failed",
  });
}

async function testRealSidecarEncodedIdRoundTrip(proxy, baseUrl) {
  const options = {
    baseUrl,
    configuredProjectId: "repo-a",
    validateDashboardSession: async () => true,
    fetchUpstream: fetch,
  };
  const query = await proxy(
    new Request(
      "http://dashboard.local/api/sidecar/v1/memories/query?app_id=forged&trace=forged",
      {
        method: "POST",
        headers: jsonHeaders(),
        body: JSON.stringify({ app_id: "forged", page_size: 20 }),
      },
    ),
    "/v1/memories/query",
    options,
  );
  assert.equal(query.status, 200);
  assert.equal((await query.json()).results[0].id, "memory/one");

  const detail = await proxy(
    new Request(
      "http://dashboard.local/api/sidecar/v1/memories/memory%2Fone?app_id=forged&trace=forged",
      { method: "GET" },
    ),
    "/v1/memories/memory/one",
    options,
  );
  assert.equal(detail.status, 200);
  assert.equal((await detail.json()).id, "memory/one");

  const history = await proxy(
    new Request(
      "http://dashboard.local/api/sidecar/v1/memories/memory%2Fone/history?app_id=forged",
      { method: "GET" },
    ),
    "/v1/memories/memory/one/history",
    options,
  );
  assert.equal(history.status, 200);
  assert.deepEqual(await history.json(), { results: [{ event: "UPDATE" }] });
}

async function testCategoryCollectionPostForcesConfiguredProject(proxy) {
  const calls = [];
  const payload = {
    name: "Support request",
    description: "Customer support request",
    schema: { type: "object" },
  };
  const response = await proxy(
    new Request(
      "http://dashboard.local/api/sidecar/v1/projects/forged/categories?project_id=also-forged&trace=create",
      {
        method: "POST",
        headers: { "X-Request-ID": "category-create-123" },
        body: JSON.stringify(payload),
      },
    ),
    "/v1/projects/forged/categories",
    proxyOptions(async (url, init) => {
      calls.push({ url: url.toString(), init });
      return Response.json({ id: "support-request", ...payload });
    }),
  );

  assert.equal(response.status, 200);
  assert.equal(calls.length, 1);
  assert.equal(
    calls[0].url,
    "http://sidecar.internal/v1/projects/runtime%20project/categories?trace=create",
  );
  assert.equal(calls[0].init.method, "POST");
  assert.equal(calls[0].init.headers.get("Content-Type"), "application/json");
  assert.equal(
    calls[0].init.headers.get("X-Request-ID"),
    "category-create-123",
  );
  assert.deepEqual(JSON.parse(calls[0].init.body), payload);
}

async function testPatchRewritesProjectEncodesCategoryAndForwardsBody(proxy) {
  const calls = [];
  const payload = { description: "Updated", enabled: false };
  const response = await proxy(
    new Request(
      "http://dashboard.local/api/sidecar/v1/projects/caller/categories/category%20one?project_id=caller&trace=yes",
      {
        method: "PATCH",
        headers: { "X-Request-ID": "request-123" },
        body: JSON.stringify(payload),
      },
    ),
    "/v1/projects/caller/categories/category one",
    proxyOptions(async (url, init) => {
      calls.push({ url: url.toString(), init });
      return Response.json({ id: "category one", ...payload });
    }),
  );

  assert.equal(response.status, 200);
  assert.equal(calls.length, 1);
  assert.equal(
    calls[0].url,
    "http://sidecar.internal/v1/projects/runtime%20project/categories/category%20one?trace=yes",
  );
  assert.equal(calls[0].init.method, "PATCH");
  assert.equal(calls[0].init.headers.get("X-Request-ID"), "request-123");
  assert.deepEqual(JSON.parse(calls[0].init.body), payload);
}

async function testDeleteForwardsNoContent(proxy) {
  const response = await proxy(
    new Request("http://dashboard.local/api/sidecar/category", {
      method: "DELETE",
    }),
    "/v1/projects/caller/categories/category-1",
    proxyOptions(async () => new Response(null, { status: 204 })),
  );

  assert.equal(response.status, 204);
  assert.equal(await response.text(), "");
}

async function testDeleteForwardsParsedError(proxy) {
  const response = await proxy(
    new Request("http://dashboard.local/api/sidecar/category", {
      method: "DELETE",
    }),
    "/v1/projects/caller/categories/missing",
    proxyOptions(async () =>
      Response.json({ detail: "Category not found" }, { status: 404 }),
    ),
  );

  assert.equal(response.status, 404);
  assert.deepEqual(await response.json(), { detail: "Category not found" });
}

async function testExportDeleteIsRejected(proxy) {
  let fetchCalled = false;
  const response = await proxy(
    new Request("http://dashboard.local/api/sidecar/v1/exports/export-1", {
      method: "DELETE",
    }),
    "/v1/exports/export-1",
    proxyOptions(async () => {
      fetchCalled = true;
      return new Response(null, { status: 204 });
    }),
  );

  assert.equal(response.status, 403);
  assert.deepEqual(await response.json(), {
    error: "Sidecar route is not allowed",
  });
  assert.equal(fetchCalled, false);
}

async function testExportPostForcesConfiguredProjectInBodyAndQuery(proxy) {
  const calls = [];
  const payload = {
    project_id: "forged-body-project",
    app_id: "forged-body-app",
    filters: { user_id: "customer-1" },
    format: "json",
  };
  const response = await proxy(
    new Request(
      "http://dashboard.local/api/sidecar/v1/exports?project_id=forged-query-project&trace=export",
      {
        method: "POST",
        headers: jsonHeaders({ "X-Request-ID": "export-create-123" }),
        body: JSON.stringify(payload),
      },
    ),
    "/v1/exports",
    proxyOptions(async (url, init) => {
      calls.push({ url: url.toString(), init });
      return Response.json({ id: "export-1", status: "pending" });
    }),
  );

  assert.equal(response.status, 200);
  assert.equal(calls.length, 1);
  assert.equal(
    calls[0].url,
    "http://sidecar.internal/v1/exports?trace=export&project_id=runtime+project",
  );
  assert.equal(calls[0].init.method, "POST");
  assert.equal(calls[0].init.headers.get("Content-Type"), "application/json");
  assert.equal(calls[0].init.headers.get("X-Request-ID"), "export-create-123");
  assert.deepEqual(JSON.parse(calls[0].init.body), {
    filters: payload.filters,
    format: payload.format,
    project_id: "runtime project",
  });
}

async function testExportListForcesConfiguredProjectInQuery(proxy) {
  const calls = [];
  const response = await proxy(
    new Request(
      "http://dashboard.local/api/sidecar/v1/exports?project_id=forged-query-project&limit=25",
      {
        method: "GET",
        headers: { "X-Request-ID": "export-list-123" },
      },
    ),
    "/v1/exports",
    proxyOptions(async (url, init) => {
      calls.push({ url: url.toString(), init });
      return Response.json({ exports: [] });
    }),
  );

  assert.equal(response.status, 200);
  assert.equal(calls.length, 1);
  assert.equal(
    calls[0].url,
    "http://sidecar.internal/v1/exports?limit=25&project_id=runtime+project",
  );
  assert.equal(calls[0].init.method, "GET");
  assert.equal(calls[0].init.headers.get("Content-Type"), "application/json");
  assert.equal(calls[0].init.headers.get("X-Request-ID"), "export-list-123");
  assert.equal(calls[0].init.body, undefined);
}

async function main() {
  if (process.argv.length !== 3) {
    throw new Error("usage: test-sidecar-proxy.cjs <dashboard-dir>");
  }
  const dashboardDir = path.resolve(process.argv[2]);
  const { proxySidecarRequest } = await loadProxyModule(dashboardDir);

  await testEntityQueryForcesRuntimeScope(proxySidecarRequest);
  await testEntityItemsEncodeTypeAndIdAndForceRuntimeScope(proxySidecarRequest);
  await testEntityRoutesRejectRebuildAndUnsafeItemPaths(proxySidecarRequest);
  await testUnsupportedSafeEntityTypePassesBackendValidation(
    proxySidecarRequest,
  );
  await testUnauthenticatedEntityRequestsAreRejected(proxySidecarRequest);
  await testEventQueryForcesRuntimeScope(proxySidecarRequest);
  await testEventDetailEncodesIdAndForcesRuntimeScope(proxySidecarRequest);
  await testEventRoutesRejectMutationsAndNearMatches(proxySidecarRequest);
  await testEventDetailRejectsUnsafeEncodedIds(proxySidecarRequest);
  await testEventRoutesFailClosedWithoutPortableRuntimeScope(
    proxySidecarRequest,
  );
  await testEventRoutesAllowServerDefaultAppResolution(proxySidecarRequest);
  await testUnauthenticatedEventRequestsAreRejected(proxySidecarRequest);
  await testUnauthenticatedAllowedRouteHidesMissingBaseUrl(proxySidecarRequest);
  await testUnauthenticatedRouteDoesNotInspectUnsafeProjectConfig(
    proxySidecarRequest,
  );
  await testUnsupportedRouteWinsBeforeAuthAndConfiguration(proxySidecarRequest);
  await testScopedJsonRoutesRequireJsonMediaType(proxySidecarRequest);
  await testEventQueryRejectsInvalidJsonAndArrayBodies(proxySidecarRequest);
  await testAuthDisabledEventQueryRejectsDeclaredOversizedJson(
    proxySidecarRequest,
  );
  await testAuthDisabledEventQueryRejectsStreamedOversizedJson(
    proxySidecarRequest,
  );
  await testEventQueryRejectsInvalidUtf8Json(proxySidecarRequest);
  await testMemoryQueryForcesRuntimeScopeAndPreservesQuery(proxySidecarRequest);
  await testMemoryQueryUsesOnlyConfiguredAppScope(proxySidecarRequest);
  await testMemoryQueryRejectsInvalidJson(proxySidecarRequest);
  await testMemoryQueryRejectsArrayBody(proxySidecarRequest);
  await testMemoryDetailEncodesIdAndForcesRuntimeScope(proxySidecarRequest);
  await testMemoryHistoryKeepsExactSuffixAndEncodedId(proxySidecarRequest);
  await testMemoryHistoryRecoversEncodedSlashFromRequestUrl(
    proxySidecarRequest,
  );
  await testMemoryPatchPreservesFieldsAndForcesRuntimeScope(
    proxySidecarRequest,
  );
  await testMemoryPatchRejectsArrayBody(proxySidecarRequest);
  await testMemoryPatchRejectsInvalidJson(proxySidecarRequest);
  await testMemoryDeleteForcesRuntimeScope(proxySidecarRequest);
  await testMemoryRoutesRejectTraversalDoubleEncodingAndExtraSegments(
    proxySidecarRequest,
  );
  await testUnauthenticatedMemoryRequestIsRejected(proxySidecarRequest);
  await testUpstreamFailureDoesNotLeakInternalDetails(proxySidecarRequest);
  await testCategoryCollectionPostForcesConfiguredProject(proxySidecarRequest);
  await testPatchRewritesProjectEncodesCategoryAndForwardsBody(
    proxySidecarRequest,
  );
  await testDeleteForwardsNoContent(proxySidecarRequest);
  await testDeleteForwardsParsedError(proxySidecarRequest);
  await testExportDeleteIsRejected(proxySidecarRequest);
  await testExportPostForcesConfiguredProjectInBodyAndQuery(
    proxySidecarRequest,
  );
  await testExportListForcesConfiguredProjectInQuery(proxySidecarRequest);
  console.log("sidecar proxy request harness: 41 contracts passed");
  const integrationBaseUrl = process.env.SIDECAR_PROXY_INTEGRATION_URL;
  if (integrationBaseUrl) {
    await testRealSidecarEncodedIdRoundTrip(
      proxySidecarRequest,
      integrationBaseUrl,
    );
    console.log("sidecar proxy integration: 3 contracts passed");
  }
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
