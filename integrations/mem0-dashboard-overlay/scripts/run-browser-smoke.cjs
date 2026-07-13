#!/usr/bin/env node
"use strict";

const cdpBase = process.env.MEM0_E2E_BROWSER_CDP || "http://browser:9222";
const dashboardBase =
  process.env.MEM0_E2E_DASHBOARD_URL || "http://dashboard:3000";

let assertions = 0;

function check(condition, message) {
  if (!condition) throw new Error(message);
  assertions += 1;
}

function sleep(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

async function waitForBrowser() {
  let lastError;
  for (let attempt = 0; attempt < 100; attempt += 1) {
    try {
      const response = await fetch(`${cdpBase}/json/version`);
      if (response.ok) return;
      lastError = new Error(`CDP readiness returned ${response.status}`);
    } catch (error) {
      lastError = error;
    }
    await sleep(200);
  }
  throw lastError || new Error("Chromium CDP did not become ready");
}

class CdpSession {
  constructor(webSocketUrl) {
    this.socket = new WebSocket(webSocketUrl);
    this.nextId = 1;
    this.pending = new Map();
  }

  async open() {
    await new Promise((resolve, reject) => {
      const timeout = setTimeout(
        () => reject(new Error("CDP WebSocket open timed out")),
        5000,
      );
      this.socket.addEventListener("open", () => {
        clearTimeout(timeout);
        resolve();
      });
      this.socket.addEventListener("error", (event) => {
        clearTimeout(timeout);
        reject(new Error(`CDP WebSocket error: ${String(event)}`));
      });
    });
    this.socket.addEventListener("message", (event) => {
      const message = JSON.parse(String(event.data));
      if (typeof message.id !== "number") return;
      const pending = this.pending.get(message.id);
      if (!pending) return;
      this.pending.delete(message.id);
      if (message.error) {
        pending.reject(new Error(message.error.message));
      } else {
        pending.resolve(message.result || {});
      }
    });
  }

  send(method, params = {}) {
    const id = this.nextId;
    this.nextId += 1;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.socket.send(JSON.stringify({ id, method, params }));
    });
  }

  close() {
    this.socket.close();
  }
}

function installBrowserMocks() {
  const originalFetch = window.fetch.bind(window);
  const NativeXMLHttpRequest = window.XMLHttpRequest;
  const record = (value) => {
    const current = sessionStorage.getItem("browser-smoke-log") || "";
    sessionStorage.setItem("browser-smoke-log", `${current}${value}\n`);
  };
  window.addEventListener("error", (event) => {
    record(`window-error:${event.message}`);
  });
  window.addEventListener("unhandledrejection", (event) => {
    record(`unhandled:${String(event.reason)}`);
  });

  class SmokeXMLHttpRequest extends EventTarget {
    constructor() {
      super();
      this.onreadystatechange = null;
      this.onload = null;
      this.onloadend = null;
      this.onerror = null;
      this.readyState = 0;
      this.response = null;
      this.responseText = "";
      this.responseType = "";
      this.status = 0;
      this.statusText = "";
      this.timeout = 0;
      this.upload = new EventTarget();
      this.withCredentials = false;
      this.native = null;
      this.mockAuth = false;
    }

    open(method, url, ...rest) {
      record(`xhr:${method}:${String(url)}`);
      this.mockAuth = String(url).includes("/auth/me");
      if (this.mockAuth) {
        this.readyState = 1;
        this.onreadystatechange?.(new Event("readystatechange"));
        return;
      }
      this.native = new NativeXMLHttpRequest();
      this.native.open(method, url, ...rest);
    }

    setRequestHeader(name, value) {
      this.native?.setRequestHeader(name, value);
    }

    getAllResponseHeaders() {
      return this.mockAuth ? "content-type: application/json\r\n" :
        (this.native?.getAllResponseHeaders() || "");
    }

    getResponseHeader(name) {
      return name.toLowerCase() === "content-type" && this.mockAuth
        ? "application/json"
        : (this.native?.getResponseHeader(name) || null);
    }

    abort() {
      this.native?.abort();
    }

    send(body) {
      if (!this.mockAuth) {
        const native = this.native;
        native.responseType = this.responseType;
        native.timeout = this.timeout;
        native.withCredentials = this.withCredentials;
        native.onreadystatechange = (event) => {
          this.readyState = native.readyState;
          this.onreadystatechange?.(event);
        };
        native.onload = (event) => this.onload?.(event);
        native.onloadend = (event) => this.onloadend?.(event);
        native.onerror = (event) => this.onerror?.(event);
        native.send(body);
        return;
      }
      const user = {
        id: "browser-user",
        name: "Browser Smoke",
        email: "browser@example.test",
        role: "admin",
        created_at: "2026-07-13T00:00:00Z",
      };
      setTimeout(() => {
        try {
          this.status = 200;
          this.statusText = "OK";
          this.readyState = 4;
          this.responseText = JSON.stringify(user);
          this.response = this.responseType === "json" ? user : this.responseText;
          record(`xhr:auth-me:complete:${this.responseType}`);
          this.onreadystatechange?.(new Event("readystatechange"));
          this.onload?.(new Event("load"));
          this.onloadend?.(new Event("loadend"));
        } catch (error) {
          record(`xhr:auth-me:error:${String(error)}`);
          this.onerror?.(new Event("error"));
          this.onloadend?.(new Event("loadend"));
        }
      }, 0);
    }
  }

  window.XMLHttpRequest = SmokeXMLHttpRequest;

  window.__sidecarSmoke = {
    mode: sessionStorage.getItem("browser-smoke-mode") || "normal",
    memoryQueries: 0,
  };

  const json = (value, status = 200) =>
    new Response(JSON.stringify(value), {
      status,
      headers: { "Content-Type": "application/json" },
    });
  const wait = (milliseconds, signal) =>
    new Promise((resolve, reject) => {
      const timer = setTimeout(resolve, milliseconds);
      if (!signal) return;
      const abort = () => {
        clearTimeout(timer);
        reject(new DOMException("Aborted", "AbortError"));
      };
      if (signal.aborted) abort();
      else signal.addEventListener("abort", abort, { once: true });
    });
  const memory = (id, content) => ({
    id,
    memory: content,
    user_id: "Alice-01",
    agent_id: "agent-1",
    app_id: "smoke-app",
    run_id: "run-1",
    categories: ["smoke"],
    metadata: { source: "browser-smoke" },
    created_at: "2026-07-13T10:00:00Z",
    updated_at: "2026-07-13T11:00:00Z",
    expiration_date: null,
  });
  const entity = {
    id: "entity-row-1",
    type: "user",
    entity_id: "Alice-01",
    display_name: "Alice smoke",
    memory_count: 2,
    last_seen_at: "2026-06-01T10:00:00Z",
    updated_at: "2026-07-13T11:00:00Z",
  };
  const trace = (operation) => {
    const display = operation || "OTHER";
    return {
      id: `trace-${display.toLowerCase().replaceAll("_", "-")}`,
      correlation_id: "browser-smoke-correlation",
      operation: display === "OTHER" ? "memory.list" : `memory.${display.toLowerCase()}`,
      display_operation: display,
      status: "SUCCEEDED",
      entities: [
        { type: "user", id: "Alice-01" },
        { type: "app", id: "smoke-app" },
      ],
      request: display === "SEARCH" ? { query: "slow search" } : {},
      response: {},
      error: {},
      result_count: 1,
      has_results: true,
      latency_ms: 12,
      requested_at: "2026-07-13T12:00:00Z",
      completed_at: "2026-07-13T12:00:00Z",
      result_previews: [],
      result_previews_omitted: 0,
      result_previews_scan_truncated: false,
    };
  };

  window.fetch = async (input, init = {}) => {
    const url = String(input instanceof Request ? input.url : input);
    const method = String(init.method || (input instanceof Request ? input.method : "GET"));
    record(`fetch:${method}:${url}`);
    if (url.includes("/_next/") || url.includes("__nextjs")) {
      return originalFetch(input, init);
    }
    if (url.includes("/api/auth/refresh")) {
      return json({ access_token: "browser-smoke-token" });
    }
    if (url.includes("/auth/me")) {
      return json({
        id: "browser-user",
        name: "Browser Smoke",
        email: "browser@example.test",
        role: "admin",
        created_at: "2026-07-13T00:00:00Z",
      });
    }
    if (url.endsWith("/api/sidecar/config")) {
      return json({ project_id: "browser-smoke-project" });
    }
    if (!url.includes("/api/sidecar/")) {
      return originalFetch(input, init);
    }

    const state = window.__sidecarSmoke;
    if (url.includes("/v1/memories/query")) {
      state.memoryQueries += 1;
      if (state.memoryQueries === 1) await wait(900, init.signal);
      if (state.mode === "error") return json({ detail: "smoke failure" }, 503);
      const results = state.mode === "empty"
        ? []
        : [memory("mem-1", "Memory alpha"), memory("mem-2", "Memory beta")];
      return json({
        results,
        page: 1,
        page_size: 20,
        total: results.length,
        has_more: false,
        stale_skipped: 0,
      });
    }
    if (url.includes("/v1/entities/query")) {
      await wait(120, init.signal);
      if (state.mode === "error") return json({ detail: "smoke failure" }, 503);
      const results = state.mode === "empty" ? [] : [entity];
      return json({ results, page: 1, page_size: 20, total: results.length, has_more: false });
    }
    if (url.includes("/v1/events/query")) {
      const body = JSON.parse(String(init.body || "{}"));
      const operation = body.operation || "OTHER";
      await wait(operation === "SEARCH" ? 800 : operation === "ADD" ? 80 : 120, init.signal);
      if (state.mode === "error") return json({ detail: "smoke failure" }, 503);
      const item = trace(operation);
      return json({
        results: state.mode === "empty" ? [] : [item],
        total: state.mode === "empty" ? 0 : 1,
        page: 1,
        page_size: 20,
        has_more: false,
        timeline: [{ timestamp: "2026-07-13T12:00:00Z", count: 1 }],
      });
    }
    if (/\/v1\/memories\/[^/]+\/history/.test(url)) {
      return json({ results: [] });
    }
    if (/\/v1\/memories\/[^/]+/.test(url)) {
      const id = decodeURIComponent(url.split("/v1/memories/")[1].split(/[/?]/)[0]);
      if (method === "DELETE") return json({ deleted: true });
      return json(memory(id, id === "mem-1" ? "Memory alpha" : "Memory beta"));
    }
    if (/\/v1\/entities\/[^/]+\/[^/?]+/.test(url)) {
      if (method === "DELETE") {
        return json({
          status: "SUCCEEDED",
          requested_count: 2,
          deleted_count: 2,
          failed_count: 0,
          failed: [],
          event_id: "entity-delete-event",
        });
      }
      return json(entity);
    }
    if (/\/v1\/events\/[^/?]+/.test(url)) {
      const id = decodeURIComponent(url.split("/v1/events/")[1].split(/[/?]/)[0]);
      return json({ ...trace(id.includes("add") ? "ADD" : "OTHER"), id });
    }
    return json({ detail: `Unhandled browser smoke route: ${url}` }, 500);
  };
}

async function openTarget() {
  const response = await fetch(`${cdpBase}/json/new?about%3Ablank`, {
    method: "PUT",
  });
  if (!response.ok) throw new Error(`Could not create CDP target: ${response.status}`);
  return response.json();
}

async function main() {
  await waitForBrowser();
  const target = await openTarget();
  const cdp = new CdpSession(target.webSocketDebuggerUrl);
  await cdp.open();
  try {
    await cdp.send("Page.enable");
    await cdp.send("Runtime.enable");
    await cdp.send("Network.enable");
    const cookie = await cdp.send("Network.setCookie", {
      name: "mem0_refresh_token",
      value: "browser-smoke-refresh-token",
      url: dashboardBase,
      httpOnly: true,
      sameSite: "Lax",
    });
    check(cookie.success !== false, "browser smoke auth cookie was rejected");
    await cdp.send("Page.addScriptToEvaluateOnNewDocument", {
      source: `(${installBrowserMocks.toString()})();`,
    });
    await cdp.send("Emulation.setDeviceMetricsOverride", {
      width: 1440,
      height: 900,
      deviceScaleFactor: 1,
      mobile: false,
    });

    const evaluate = async (expression) => {
      const response = await cdp.send("Runtime.evaluate", {
        expression,
        awaitPromise: true,
        returnByValue: true,
      });
      if (response.exceptionDetails) {
        throw new Error(response.exceptionDetails.text || "Browser evaluation failed");
      }
      return response.result.value;
    };
    const waitFor = async (expression, label, timeout = 15000) => {
      const deadline = Date.now() + timeout;
      while (Date.now() < deadline) {
        if (await evaluate(expression)) return;
        await sleep(100);
      }
      const diagnostic = await evaluate(`({
        body: document.body?.innerText?.slice(0, 1200) || '',
        log: sessionStorage.getItem('browser-smoke-log') || ''
      })`);
      throw new Error(
        `Timed out waiting for ${label}; diagnostic=${JSON.stringify(diagnostic)}`,
      );
    };
    const waitText = (value, timeout) =>
      waitFor(
        `document.body?.innerText?.includes(${JSON.stringify(value)}) === true`,
        JSON.stringify(value),
        timeout,
      );
    const clickButton = async (label) => {
      const clicked = await evaluate(`(() => {
        const target = [...document.querySelectorAll('button')].find(
          (item) => item.innerText.trim() === ${JSON.stringify(label)}
            || item.getAttribute('aria-label') === ${JSON.stringify(label)}
        );
        if (!target) return false;
        target.click();
        return true;
      })()`);
      check(clicked, `button not found: ${label}`);
    };
    const clickAria = async (label) => {
      const clicked = await evaluate(`(() => {
        const target = document.querySelector(${JSON.stringify(`[aria-label="${label}"]`)});
        if (!target) return false;
        target.click();
        return true;
      })()`);
      check(clicked, `aria control not found: ${label}`);
    };
    const setMode = (mode) =>
      evaluate(`(() => {
        const mode = ${JSON.stringify(mode)};
        sessionStorage.setItem('browser-smoke-mode', mode);
        window.__sidecarSmoke.mode = mode;
      })()`);
    const navigate = async (path) => {
      await cdp.send("Page.navigate", { url: `${dashboardBase}${path}` });
    };

    await navigate("/dashboard/memories");
    await waitText("Loading memories...", 30000);
    check(true, "memory loading state rendered");
    await waitText("Memory alpha");
    check(true, "memory results rendered");

    await clickAria("Edit filters");
    await waitText("Add filter");
    check(true, "filter popover rendered");
    await clickButton("Cancel");
    await clickAria("Choose date range: All time");
    await waitText("Last 24 hours");
    check(true, "date range popover rendered");
    await clickButton("Cancel");

    await clickAria("Open memory mem-1");
    await waitText("Memory details");
    await waitText("Memory alpha");
    check(true, "memory drawer loaded real detail content");
    await clickButton("Delete");
    await waitText("Delete memory");
    const memoryDeleteDisabled = await evaluate(`(() => {
      const dialog = [...document.querySelectorAll('[role="dialog"]')].find(
        (item) => item.innerText.includes('Delete memory')
      );
      const button = dialog && [...dialog.querySelectorAll('button')].find(
        (item) => item.innerText.trim() === 'Delete'
      );
      return Boolean(button?.disabled);
    })()`);
    check(memoryDeleteDisabled, "memory destructive confirmation was not guarded");
    await clickButton("Cancel");
    await clickButton("Close");

    await setMode("empty");
    await navigate("/dashboard/entities");
    await waitText("No entities found.", 30000);
    check(true, "entity empty state rendered");
    await setMode("normal");
    await clickButton("Refresh");
    await waitText("Alice smoke");
    check(true, "entity results recovered from empty state");
    await clickAria("Delete user entity Alice-01");
    await waitText("Delete entity and its memories?");
    const initiallyDisabled = await evaluate(`(() => {
      const dialog = [...document.querySelectorAll('[role="alertdialog"]')].find(
        (item) => item.innerText.includes('Delete entity and its memories?')
      );
      const button = dialog && [...dialog.querySelectorAll('button')].find(
        (item) => item.innerText.trim() === 'Delete entity'
      );
      return Boolean(button?.disabled);
    })()`);
    check(initiallyDisabled, "entity deletion allowed without typed confirmation");
    await evaluate(`(() => {
      const input = document.querySelector('#entity-delete-confirmation');
      const setter = Object.getOwnPropertyDescriptor(
        HTMLInputElement.prototype, 'value'
      ).set;
      setter.call(input, 'Alice-01');
      input.dispatchEvent(new Event('input', { bubbles: true }));
    })()`);
    await waitFor(`(() => {
      const dialog = [...document.querySelectorAll('[role="alertdialog"]')].find(
        (item) => item.innerText.includes('Delete entity and its memories?')
      );
      const button = dialog && [...dialog.querySelectorAll('button')].find(
        (item) => item.innerText.trim() === 'Delete entity'
      );
      return button && !button.disabled;
    })()`, "exact entity confirmation to enable delete");
    check(true, "entity destructive confirmation required exact ID");
    await clickButton("Cancel");

    await setMode("error");
    await navigate("/dashboard/requests");
    await waitText("Could not load requests: smoke failure");
    check(true, "request error state rendered");
    await setMode("normal");
    await clickButton("Retry");
    await waitText("Request timeline");
    check(true, "request list recovered from error state");

    await clickButton("SEARCH");
    await sleep(40);
    await clickButton("ADD");
    await waitText("Add memory");
    await sleep(900);
    const replacementIsCurrent = await evaluate(
      `document.body.innerText.includes('Add memory') && !document.body.innerText.includes('slow search')`,
    );
    check(replacementIsCurrent, "deferred request replaced the newer result");

    const openerPrepared = await evaluate(`(() => {
      const target = document.querySelector('[aria-label^="Open request trace-add"]');
      if (!target) return false;
      target.dataset.smokeOpener = 'true';
      target.focus();
      return document.activeElement === target;
    })()`);
    check(openerPrepared, "request keyboard opener was not focusable");
    await cdp.send("Input.dispatchKeyEvent", {
      type: "rawKeyDown",
      key: " ",
      code: "Space",
      windowsVirtualKeyCode: 32,
      nativeVirtualKeyCode: 32,
      text: " ",
      unmodifiedText: " ",
    });
    await cdp.send("Input.dispatchKeyEvent", {
      type: "keyUp",
      key: " ",
      code: "Space",
      windowsVirtualKeyCode: 32,
    });
    await waitText("Inspect the sanitized request payload");
    check(true, "keyboard activation opened the request drawer");
    await clickButton("Close");
    await waitFor(
      `document.activeElement?.dataset?.smokeOpener === 'true'`,
      "request drawer focus restoration",
    );
    check(true, "request drawer restored focus to its opener");

    await cdp.send("Emulation.setDeviceMetricsOverride", {
      width: 390,
      height: 844,
      deviceScaleFactor: 1,
      mobile: true,
    });
    await navigate("/dashboard/memories");
    await waitText("Memory alpha", 30000);
    const mobilePrepared = await evaluate(`(() => {
      const target = [...document.querySelectorAll('button')].find(
        (item) => item.innerText.includes('Memory alpha')
      );
      if (!target) return false;
      target.focus();
      return document.activeElement === target;
    })()`);
    check(mobilePrepared, "narrow memory row was not keyboard focusable");
    await cdp.send("Input.dispatchKeyEvent", {
      type: "rawKeyDown",
      key: " ",
      code: "Space",
      windowsVirtualKeyCode: 32,
      nativeVirtualKeyCode: 32,
      text: " ",
      unmodifiedText: " ",
    });
    await cdp.send("Input.dispatchKeyEvent", {
      type: "keyUp",
      key: " ",
      code: "Space",
      windowsVirtualKeyCode: 32,
    });
    await waitText("Memory details");
    await waitFor(`(() => {
      const dialog = [...document.querySelectorAll('[role="dialog"]')].find(
        (item) => item.innerText.includes('Memory details')
      );
      const rect = dialog?.getBoundingClientRect();
      return Boolean(rect && rect.left >= -1 && rect.right <= window.innerWidth + 1);
    })()`, "settled narrow memory drawer", 3000);
    const responsive = await evaluate(`(() => {
      const dialog = [...document.querySelectorAll('[role="dialog"]')].find(
        (item) => item.innerText.includes('Memory details')
      );
      const rect = dialog?.getBoundingClientRect();
      return {
        dialogLeft: rect?.left ?? null,
        dialogRight: rect?.right ?? null,
        innerWidth: window.innerWidth,
        documentWidth: document.documentElement.scrollWidth,
        bodyWidth: document.body.scrollWidth,
      };
    })()`);
    check(
      responsive.dialogLeft !== null
        && responsive.dialogLeft >= -1
        && responsive.dialogRight <= responsive.innerWidth + 1
        && responsive.documentWidth <= responsive.innerWidth
        && responsive.bodyWidth <= responsive.innerWidth,
      `narrow drawer or page leaked horizontal overflow: ${JSON.stringify(responsive)}`,
    );

    console.log(
      `Browser smoke passed: ${assertions} behavior assertions across desktop and narrow viewports`,
    );
  } finally {
    cdp.close();
  }
}

main().catch((error) => {
  console.error(error && error.stack ? error.stack : error);
  process.exitCode = 1;
});
