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

function proxyOptions(fetchUpstream) {
  return {
    baseUrl: "http://sidecar.internal",
    configuredProjectId: "runtime project",
    validateDashboardSession: async () => true,
    fetchUpstream,
  };
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
    filters: { user_id: "customer-1" },
    format: "json",
  };
  const response = await proxy(
    new Request(
      "http://dashboard.local/api/sidecar/v1/exports?project_id=forged-query-project&trace=export",
      {
        method: "POST",
        headers: { "X-Request-ID": "export-create-123" },
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
  assert.equal(
    calls[0].init.headers.get("X-Request-ID"),
    "export-create-123",
  );
  assert.deepEqual(JSON.parse(calls[0].init.body), {
    ...payload,
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

  await testCategoryCollectionPostForcesConfiguredProject(
    proxySidecarRequest,
  );
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
  console.log("sidecar proxy request harness: 8 contracts passed");
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
