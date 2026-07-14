#!/usr/bin/env node
"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const smokePath = path.join(__dirname, "run-browser-smoke.cjs");
const source = fs.readFileSync(smokePath, "utf8");
const boundary = source.indexOf("\nasync function openTarget()");
assert.notEqual(boundary, -1, "browser smoke mock boundary is missing");

const storage = new Map();
const sessionStorage = {
  getItem(key) {
    return storage.has(key) ? storage.get(key) : null;
  },
  setItem(key, value) {
    storage.set(key, String(value));
  },
};
class NativeXMLHttpRequest {}
const originalFetch = async () => new Response("original", { status: 200 });
const window = {
  addEventListener() {},
  fetch: originalFetch,
  location: { href: "http://dashboard:3000/dashboard/requests" },
  XMLHttpRequest: NativeXMLHttpRequest,
};
const context = vm.createContext({
  AbortController,
  DOMException,
  Event,
  EventTarget,
  NativeXMLHttpRequest,
  Request,
  Response,
  URL,
  clearTimeout,
  console,
  process: { env: {} },
  sessionStorage,
  setTimeout,
  window,
});
vm.runInContext(
  `${source.slice(0, boundary)}\n;globalThis.installSmokeMocks = installBrowserMocks;`,
  context,
  { filename: smokePath },
);
context.installSmokeMocks();

async function main() {
  const query = await window.fetch(
    "http://dashboard:3000/api/sidecar/v1/events/query",
    { method: "POST", body: JSON.stringify({ operation: "ADD" }) },
  );
  assert.equal(query.status, 200);
  const queryBody = await query.json();

  const encodedDetailPath = `/api/sidecar/v1/event/${encodeURIComponent(queryBody.results[0].id)}`;
  const detail = await window.fetch(
    `http://dashboard:3000${encodedDetailPath}`,
  );
  assert.equal(
    detail.status,
    200,
    "singular encoded request-detail route must be handled",
  );
  assert.equal(queryBody.results[0].id, "trace-add/detail");
  const detailBody = await detail.json();
  assert.equal(detailBody.id, "trace-add/detail");
  assert.equal(
    detailBody.correlation_id,
    "browser-smoke-detail-correlation-from-response",
  );
  assert.equal(
    detailBody.request.query,
    "browser-smoke-detail-query-from-response",
  );
  assert.deepEqual(detailBody.result_previews, [
    {
      id: "browser-smoke-detail-preview-from-response",
      memory: "Browser smoke retrieved preview from detail response",
    },
  ]);
  assert.deepEqual([...window.__sidecarSmoke.unhandledRoutes], []);

  const pluralDetail = await window.fetch(
    `http://dashboard:3000/api/sidecar/v1/events/${encodeURIComponent(queryBody.results[0].id)}`,
  );
  assert.equal(
    pluralDetail.status,
    500,
    "plural item route must remain unhandled",
  );
  assert.deepEqual(
    [...window.__sidecarSmoke.unhandledRoutes],
    [
      `GET:/api/sidecar/v1/events/${encodeURIComponent(queryBody.results[0].id)}`,
    ],
  );
  context.installSmokeMocks();
  assert.deepEqual(
    [...window.__sidecarSmoke.unhandledRoutes],
    [
      `GET:/api/sidecar/v1/events/${encodeURIComponent(queryBody.results[0].id)}`,
    ],
    "mock-route failures must survive navigation-time mock reinstallation",
  );

  console.log(
    "browser smoke mock contract: singular encoded detail route passed",
  );
}

main().catch((error) => {
  console.error(error && error.stack ? error.stack : error);
  process.exitCode = 1;
});
