#!/usr/bin/env node
"use strict";

// Real destructive browser acceptance: no browser response interception or mocks.

const fs = require("node:fs");

const cdpBase = process.env.MEM0_E2E_BROWSER_CDP || "http://browser:9222";
const dashboardBase = (
  process.env.MEM0_E2E_DASHBOARD_URL || "http://dashboard:3000"
).replace(/\/$/, "");
const authDashboardBase = (
  process.env.MEM0_E2E_AUTH_DASHBOARD_URL || "http://dashboard-auth-check:3000"
).replace(/\/$/, "");
const sidecarBase = (
  process.env.MEM0_E2E_SIDECAR_URL || "http://sidecar:8765"
).replace(/\/$/, "");
const mem0Base = (process.env.MEM0_E2E_MEM0_URL || "http://mem0:8000").replace(
  /\/$/,
  "",
);
const projectId = process.env.MEM0_E2E_PROJECT_ID || "sidecar-e2e";
const appId = process.env.MEM0_E2E_APP_ID || "sidecar-e2e-app";
const browserEvidenceDir =
  process.env.MEM0_E2E_BROWSER_EVIDENCE_DIR || "/evidence";

function sleep(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

function errorMessage(error) {
  return error && error.stack ? error.stack : String(error);
}

async function fetchWithTimeout(url, options = {}) {
  const signal = AbortSignal.timeout(options.timeout || 30000);
  return fetch(url, { ...options, signal, timeout: undefined });
}

async function responseDiagnostic(response) {
  const body = await response.text().catch(() => "<unreadable response>");
  return `HTTP ${response.status} ${response.url}: ${body.slice(0, 1200)}`;
}

async function waitForBrowser() {
  let lastError;
  for (let attempt = 0; attempt < 100; attempt += 1) {
    try {
      const response = await fetchWithTimeout(`${cdpBase}/json/version`, {
        timeout: 2000,
      });
      if (response.ok) return;
      lastError = new Error(await responseDiagnostic(response));
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
    this.listeners = new Map();
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
      if (typeof message.id !== "number") {
        for (const listener of this.listeners.get(message.method) || []) {
          listener(message.params || {});
        }
        return;
      }
      const pending = this.pending.get(message.id);
      if (!pending) return;
      this.pending.delete(message.id);
      if (message.error) pending.reject(new Error(message.error.message));
      else pending.resolve(message.result || {});
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

  on(method, listener) {
    const listeners = this.listeners.get(method) || [];
    listeners.push(listener);
    this.listeners.set(method, listeners);
  }

  close() {
    this.socket.close();
  }
}

async function openTarget() {
  const response = await fetchWithTimeout(`${cdpBase}/json/new?about%3Ablank`, {
    method: "PUT",
    timeout: 5000,
  });
  if (!response.ok) throw new Error(await responseDiagnostic(response));
  return response.json();
}

async function captureBrowserEvidence(cdp, filename) {
  await cdp.send("Runtime.evaluate", {
    expression: `document.querySelectorAll("nextjs-portal").forEach(
      (portal) => portal.style.setProperty("display", "none", "important"),
    )`,
  });
  const screenshot = await cdp.send("Page.captureScreenshot", {
    format: "png",
    fromSurface: true,
    captureBeyondViewport: false,
  });
  const data = Buffer.from(screenshot.data || "", "base64");
  const pngSignature = Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]);
  if (
    data.length < pngSignature.length ||
    !data.subarray(0, 8).equals(pngSignature)
  ) {
    throw new Error(`Browser evidence ${filename} was not a valid PNG`);
  }
  fs.mkdirSync(browserEvidenceDir, { recursive: true });
  fs.writeFileSync(`${browserEvidenceDir}/${filename}`, data, { mode: 0o600 });
}

async function dashboardRefreshToken() {
  const credentials = {
    name: "Browser E2E",
    email: "browser-e2e@example.com",
    password: "browser-e2e-password",
  };
  const setup = await fetchWithTimeout(`${mem0Base}/auth/setup-status`, {
    timeout: 10000,
  });
  if (!setup.ok) throw new Error(await responseDiagnostic(setup));
  const setupState = await setup.json();
  const path = setupState?.needsSetup ? "/auth/register" : "/auth/login";
  const payload = setupState?.needsSetup
    ? credentials
    : { email: credentials.email, password: credentials.password };
  const response = await fetchWithTimeout(`${mem0Base}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
    timeout: 10000,
  });
  if (!response.ok) throw new Error(await responseDiagnostic(response));
  const session = await response.json();
  if (
    typeof session?.refresh_token !== "string" ||
    session.refresh_token.length === 0
  ) {
    throw new Error("Core dashboard session omitted its refresh token");
  }
  return session.refresh_token;
}

async function setDashboardSessionPrerequisite(cdp, refreshToken) {
  if (typeof refreshToken !== "string" || refreshToken.length === 0) {
    throw new Error("A real dashboard refresh token is required");
  }
  const cookie = await cdp.send("Network.setCookie", {
    name: "mem0_refresh_token",
    value: refreshToken,
    url: dashboardBase,
    httpOnly: true,
    sameSite: "Lax",
  });
  if (cookie.success === false) {
    throw new Error("Real browser dashboard session cookie was rejected");
  }
}

async function proveUnauthenticatedClientKeysRedirect(cdp) {
  await cdp.send("Network.deleteCookies", {
    name: "mem0_refresh_token",
    url: authDashboardBase,
  });
  await cdp.send("Page.navigate", {
    url: `${authDashboardBase}/dashboard/api-keys`,
  });
  const { evaluate, waitFor } = createBrowserDriver(cdp);
  await waitFor(
    `location.origin === ${JSON.stringify(authDashboardBase)} &&
      location.pathname === "/login"`,
    "unauthenticated Client Keys redirect",
  );
  await waitFor(
    `document.readyState === "complete" &&
      document.body?.innerText?.includes("Sign in to Mem0") === true`,
    "unauthenticated login page",
  );
  const result = await evaluate(`({
    path: location.pathname,
    body: document.body?.innerText || ""
  })`);
  if (
    result?.path !== "/login" ||
    !result?.body?.includes("Sign in to Mem0") ||
    result?.body?.includes("API & MCP Client Keys")
  ) {
    throw new Error(
      `Unauthenticated Client Keys route was exposed: ${JSON.stringify(result)}`,
    );
  }
}

async function seedFixtureThroughSidecar() {
  const token = `${Date.now()}-${crypto.randomUUID()}`;
  const marker = `real-browser-destructive-${token}`;
  const response = await fetchWithTimeout(`${sidecarBase}/v3/memories/add/`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-Request-ID": `browser-seed-${token}`,
    },
    body: JSON.stringify({
      project_id: projectId,
      app_id: appId,
      user_id: `browser-user-${token}`,
      run_id: `browser-run-${token}`,
      text: marker,
      infer: false,
      metadata: { marker, e2e: "real-destructive-browser" },
    }),
  });
  if (!response.ok) throw new Error(await responseDiagnostic(response));
  const payload = await response.json();
  const memoryId = payload?.event?.subject_id;
  if (typeof memoryId !== "string" || memoryId.length === 0) {
    throw new Error(
      `Sidecar seed returned no real memory ID: ${JSON.stringify(payload)}`,
    );
  }
  return { memoryId, marker };
}

function createBrowserDriver(cdp) {
  const evaluate = async (expression) => {
    const response = await cdp.send("Runtime.evaluate", {
      expression,
      awaitPromise: true,
      returnByValue: true,
    });
    if (response.exceptionDetails) {
      const exception = response.exceptionDetails.exception;
      throw new Error(
        exception?.description ||
          response.exceptionDetails.text ||
          "Browser evaluation failed",
      );
    }
    return response.result.value;
  };

  const waitFor = async (expression, label, timeout = 60000) => {
    const deadline = Date.now() + timeout;
    let lastValue;
    while (Date.now() < deadline) {
      lastValue = await evaluate(expression);
      if (lastValue) return lastValue;
      await sleep(150);
    }
    const diagnostic = await evaluate(`({
      url: location.href,
      body: document.body?.innerText?.slice(0, 2000) || ""
    })`);
    throw new Error(
      `Timed out waiting for ${label}; last=${JSON.stringify(lastValue)}; ` +
        `diagnostic=${JSON.stringify(diagnostic)}`,
    );
  };

  return { evaluate, waitFor };
}

async function waitForVisualStability(cdp, label) {
  const { waitFor } = createBrowserDriver(cdp);
  await waitFor(
    `document.readyState === "complete" &&
      document.getAnimations().every(
        (animation) => animation.playState !== "running",
      )`,
    `${label} visual stability`,
  );
  await cdp.send("Runtime.evaluate", {
    expression:
      "new Promise((resolve) => requestAnimationFrame(() => " +
      "requestAnimationFrame(resolve)))",
    awaitPromise: true,
  });
}

async function setViewport(cdp, { width, height, mobile }) {
  const { windowId } = await cdp.send("Browser.getWindowForTarget");
  await cdp.send("Browser.setWindowBounds", {
    windowId,
    bounds: {
      width,
      height,
      windowState: "normal",
    },
  });
  await cdp.send("Emulation.setDeviceMetricsOverride", {
    width,
    height,
    deviceScaleFactor: 1,
    mobile,
    scale: 1,
    screenWidth: width,
    screenHeight: height,
    positionX: 0,
    positionY: 0,
    dontSetVisibleSize: false,
    screenOrientation: {
      type: "portraitPrimary",
      angle: 0,
    },
  });
  await cdp.send("Emulation.setVisibleSize", { width, height });
  await cdp.send("Emulation.setPageScaleFactor", { pageScaleFactor: 1 });
}

async function assertClientKeysFitViewport(cdp) {
  const { evaluate } = createBrowserDriver(cdp);
  const metricsExpression = `(() => {
      const heading = [...document.querySelectorAll("h1")].find(
        (item) => item.innerText.trim() === "API & MCP Client Keys",
      );
      const button = [...document.querySelectorAll("button")].find(
        (item) => item.innerText.trim() === "Create client key",
      );
      const revokeButtons = [
        ...document.querySelectorAll('button[aria-label^="Revoke "]'),
      ].filter((item) => item.getClientRects().length > 0);
      if (!heading || !button || revokeButtons.length === 0) {
        return { fits: false, missing: true };
      }
      const viewportWidth = document.documentElement.clientWidth;
      const innerWidth = window.innerWidth;
      const visualViewportWidth = window.visualViewport?.width ?? null;
      const screenWidth = window.screen.width;
      const headingRect = heading.getBoundingClientRect();
      const buttonRect = button.getBoundingClientRect();
      const revokeRects = revokeButtons.map((item) =>
        item.getBoundingClientRect(),
      );
      const fits =
        document.documentElement.scrollWidth <= viewportWidth &&
        innerWidth <= viewportWidth + 1 &&
        (visualViewportWidth === null ||
          visualViewportWidth <= viewportWidth + 1) &&
        screenWidth <= viewportWidth + 1 &&
        headingRect.left >= 0 &&
        headingRect.right <= viewportWidth + 1 &&
        buttonRect.left >= 0 &&
        buttonRect.right <= viewportWidth + 1 &&
        buttonRect.width > 0 &&
        revokeRects.every(
          (rect) =>
            rect.left >= 0 &&
            rect.right <= viewportWidth + 1 &&
            rect.width > 0,
        );
      const overflowing = [...document.querySelectorAll("*")]
        .map((element) => {
          const rect = element.getBoundingClientRect();
          return {
            tag: element.tagName,
            className:
              typeof element.className === "string"
                ? element.className.slice(0, 160)
                : "",
            left: Math.round(rect.left),
            right: Math.round(rect.right),
            width: Math.round(rect.width),
          };
        })
        .filter(
          (item) =>
            item.width > 0 &&
            (item.left < -1 || item.right > viewportWidth + 1),
        )
        .slice(0, 8);
      return {
        fits,
        viewportWidth,
        innerWidth,
        visualViewportWidth,
        screenWidth,
        documentWidth: document.documentElement.scrollWidth,
        headingLeft: headingRect.left,
        headingRight: headingRect.right,
        buttonLeft: buttonRect.left,
        buttonRight: buttonRect.right,
        revokeButtons: revokeRects.map((rect) => ({
          left: rect.left,
          right: rect.right,
          width: rect.width,
        })),
        overflowing,
      };
    })()`;
  const deadline = Date.now() + 10000;
  let metrics;
  while (Date.now() < deadline) {
    metrics = await evaluate(metricsExpression);
    if (metrics?.fits) {
      await cdp.send("Runtime.evaluate", {
        expression:
          "new Promise((resolve) => requestAnimationFrame(() => " +
          "requestAnimationFrame(resolve)))",
        awaitPromise: true,
      });
      const stableMetrics = await evaluate(metricsExpression);
      if (stableMetrics?.fits) return stableMetrics;
    }
    await sleep(100);
  }
  throw new Error(
    `Client Keys content did not fit the compact viewport: ` +
      JSON.stringify(metrics),
  );
}

async function listCoreClientKeys() {
  const response = await fetchWithTimeout(`${mem0Base}/api-keys`, {
    timeout: 10000,
  });
  if (!response.ok) throw new Error(await responseDiagnostic(response));
  const payload = await response.json();
  if (!Array.isArray(payload)) {
    throw new Error(
      `Core key list was not an array: ${JSON.stringify(payload)}`,
    );
  }
  return payload;
}

async function waitForCoreClientKey(label, expectedPresent) {
  const deadline = Date.now() + 30000;
  let matchingKey;
  while (Date.now() < deadline) {
    const keys = await listCoreClientKeys();
    matchingKey = keys.find((key) => key?.label === label);
    if (Boolean(matchingKey) === expectedPresent) return matchingKey || null;
    await sleep(200);
  }
  throw new Error(
    `Core client key ${label} presence remained ${String(Boolean(matchingKey))}`,
  );
}

async function createClientKeyThroughDashboard(cdp, label) {
  const { evaluate, waitFor } = createBrowserDriver(cdp);
  await cdp.send("Page.navigate", {
    url: `${dashboardBase}/dashboard/api-keys`,
  });
  await waitFor(
    `document.body?.innerText?.includes("API & MCP Client Keys") === true`,
    "Client Keys page",
  );
  const dialogOpened = await evaluate(`(() => {
    const button = [...document.querySelectorAll("button")].find(
      (item) => item.innerText.trim() === "Create client key",
    );
    if (!button) return false;
    button.click();
    return true;
  })()`);
  if (!dialogOpened) throw new Error("Create client key button was not found");
  await waitFor(
    `Boolean(document.querySelector("#api-key-label"))`,
    "client-key label input",
  );
  await waitForVisualStability(cdp, "client-key create dialog");
  await captureBrowserEvidence(cdp, "client-keys-create-dialog-desktop.png");
  const entered = await evaluate(`(() => {
    const input = document.querySelector("#api-key-label");
    if (!(input instanceof HTMLInputElement)) return false;
    const setter = Object.getOwnPropertyDescriptor(
      HTMLInputElement.prototype,
      "value",
    )?.set;
    if (!setter) return false;
    setter.call(input, ${JSON.stringify(label)});
    input.dispatchEvent(new Event("input", { bubbles: true }));
    return true;
  })()`);
  if (!entered) throw new Error("Client-key label could not be entered");
  await waitFor(
    `(() => {
      const dialog = document.querySelector('[role="dialog"]');
      const button = dialog &&
        [...dialog.querySelectorAll("button")].find(
          (item) => item.innerText.trim() === "Create",
        );
      return Boolean(button && !button.disabled);
    })()`,
    "enabled client-key create action",
  );
  const created = await evaluate(`(() => {
    const dialog = document.querySelector('[role="dialog"]');
    const button = dialog &&
      [...dialog.querySelectorAll("button")].find(
        (item) => item.innerText.trim() === "Create",
    );
    if (!button || button.disabled) return false;
    button.click();
    button.click();
    return true;
  })()`);
  if (!created) throw new Error("Client-key create action was not available");
  const secret = await waitFor(
    `(() => {
      const input = document.querySelector("#api-key-new");
      return input instanceof HTMLInputElement && input.value.startsWith("m0sk_")
        ? input.value
        : "";
    })()`,
    "one-time client key",
  );
  const copyActionAvailable = await evaluate(
    `Boolean(document.querySelector('[aria-label="Copy client key"]'))`,
  );
  if (!copyActionAvailable) {
    throw new Error("Copy client key action was not available");
  }
  const descriptor = await waitForCoreClientKey(label, true);
  const matchingKeys = (await listCoreClientKeys()).filter(
    (key) => key?.label === label,
  );
  if (matchingKeys.length !== 1) {
    throw new Error(
      `Concurrent create produced ${matchingKeys.length} keys for ${label}`,
    );
  }
  if (
    typeof descriptor?.id !== "string" ||
    typeof descriptor?.key_prefix !== "string"
  ) {
    throw new Error(
      `Core client-key descriptor is incomplete: ${JSON.stringify(descriptor)}`,
    );
  }
  return { descriptor, secret };
}

async function assertClientKeyIsOneTimeOnly(cdp, label, descriptor, secret) {
  const { evaluate, waitFor } = createBrowserDriver(cdp);
  const closed = await evaluate(`(() => {
    const dialog = document.querySelector('[role="dialog"]');
    const button = dialog &&
      [...dialog.querySelectorAll("button")].find(
        (item) => item.innerText.trim() === "Done",
      );
    if (!button) return false;
    button.click();
    return true;
  })()`);
  if (!closed)
    throw new Error("Client-key one-time dialog could not be closed");
  const revokeSelector = `[aria-label="Revoke ${label}"]`;
  await waitFor(
    `Boolean(document.querySelector(${JSON.stringify(revokeSelector)}))`,
    `listed client key ${label}`,
  );
  const persistedSecret = await evaluate(`(() => {
    const values = [];
    for (const storage of [localStorage, sessionStorage]) {
      for (let index = 0; index < storage.length; index += 1) {
        const key = storage.key(index);
        if (key !== null) values.push(storage.getItem(key) || "");
      }
    }
    return document.documentElement.innerHTML.includes(${JSON.stringify(secret)}) ||
      values.some((value) => value.includes(${JSON.stringify(secret)}));
  })()`);
  if (persistedSecret) {
    throw new Error("One-time client key remained in DOM or browser storage");
  }
  const prefixVisible = await evaluate(
    `document.body?.innerText?.includes(${JSON.stringify(`${descriptor.key_prefix}...`)}) === true`,
  );
  if (!prefixVisible)
    throw new Error(`Client key prefix was not listed for ${label}`);
  await cdp.send("Page.navigate", {
    url: `${dashboardBase}/dashboard/api-keys`,
  });
  await waitFor(
    `Boolean(document.querySelector(${JSON.stringify(revokeSelector)}))`,
    `reloaded client key ${label}`,
  );
  const secretAfterReload = await evaluate(
    `document.documentElement.innerHTML.includes(${JSON.stringify(secret)})`,
  );
  if (secretAfterReload) {
    throw new Error("One-time client key reappeared after page reload");
  }
}

async function revokeClientKeyThroughDashboard(cdp, label) {
  const { evaluate, waitFor } = createBrowserDriver(cdp);
  const revokeSelector = `[aria-label="Revoke ${label}"]`;
  const opened = await evaluate(`(() => {
    const button = document.querySelector(${JSON.stringify(revokeSelector)});
    if (!button) return false;
    button.click();
    return true;
  })()`);
  if (!opened) throw new Error(`Revoke action was not found for ${label}`);
  await waitFor(
    `document.querySelector('[role="dialog"]')?.innerText?.includes("Revoke client key") === true`,
    "Revoke client key confirmation",
  );
  const entered = await evaluate(`(() => {
    const dialog = document.querySelector('[role="dialog"]');
    const input = dialog?.querySelector('input[placeholder="Enter name to confirm"]');
    if (!(input instanceof HTMLInputElement)) return false;
    const setter = Object.getOwnPropertyDescriptor(
      HTMLInputElement.prototype,
      "value",
    )?.set;
    if (!setter) return false;
    setter.call(input, ${JSON.stringify(label)});
    input.dispatchEvent(new Event("input", { bubbles: true }));
    return true;
  })()`);
  if (!entered) throw new Error("Client-key revoke confirmation was not found");
  await waitFor(
    `(() => {
      const dialog = document.querySelector('[role="dialog"]');
      const button = dialog &&
        [...dialog.querySelectorAll("button")].find(
          (item) => item.innerText.trim() === "Revoke",
        );
      return Boolean(button && !button.disabled);
    })()`,
    "enabled client-key revoke action",
  );
  const confirmed = await evaluate(`(() => {
    const dialog = document.querySelector('[role="dialog"]');
    const button = dialog &&
      [...dialog.querySelectorAll("button")].find(
        (item) => item.innerText.trim() === "Revoke",
    );
    if (!button || button.disabled) return false;
    button.click();
    button.click();
    return true;
  })()`);
  if (!confirmed)
    throw new Error(`Client-key revoke was not enabled for ${label}`);
  await waitForCoreClientKey(label, false);
  await waitFor(
    `!document.querySelector(${JSON.stringify(revokeSelector)})`,
    `revoked client key ${label} to disappear`,
  );
}

async function cleanupClientKey(label) {
  const keys = await listCoreClientKeys();
  const matchingKeys = keys.filter((key) => key?.label === label);
  const failures = [];
  for (const key of matchingKeys) {
    if (typeof key?.id !== "string") {
      failures.push(`missing ID: ${JSON.stringify(key)}`);
      continue;
    }
    const response = await fetchWithTimeout(
      `${mem0Base}/api-keys/${encodeURIComponent(key.id)}`,
      { method: "DELETE", timeout: 10000 },
    );
    if (![200, 204, 404].includes(response.status)) {
      failures.push(await responseDiagnostic(response));
    }
  }
  if (failures.length > 0) {
    throw new Error(`Client-key cleanup failed: ${failures.join("; ")}`);
  }
}

async function openMemoryDetails(cdp, memoryId, marker) {
  const { evaluate, waitFor } = createBrowserDriver(cdp);
  await cdp.send("Page.navigate", {
    url: `${dashboardBase}/dashboard/memories`,
  });
  const ariaLabel = `Open memory ${memoryId}`;
  await waitFor(
    `Boolean(document.querySelector(${JSON.stringify(`[aria-label="${ariaLabel}"]`)}))`,
    `real memory row ${memoryId}`,
  );
  const clicked = await evaluate(`(() => {
    const target = document.querySelector(
      ${JSON.stringify(`[aria-label="${ariaLabel}"]`)},
    );
    if (!target) return false;
    target.click();
    return true;
  })()`);
  if (!clicked) throw new Error(`Could not open exact memory row ${memoryId}`);
  await waitFor(
    `document.body?.innerText?.includes("Memory details") === true &&
      document.querySelector("#memory-content")?.value === ${JSON.stringify(marker)}`,
    `real memory detail ${memoryId}`,
  );
}

async function confirmExactMemoryId(cdp, memoryId) {
  const { evaluate, waitFor } = createBrowserDriver(cdp);
  const deleteOpened = await evaluate(`(() => {
    const drawers = [...document.querySelectorAll('[role="dialog"]')].filter(
      (item) => item.innerText.includes("Memory details"),
    );
    if (drawers.length !== 1) {
      return { clicked: false, drawers: drawers.length, buttons: 0 };
    }
    const drawer = drawers[0];
    const buttons = [...drawer.querySelectorAll("button")].filter(
      (item) => item.innerText.trim() === "Delete",
    );
    if (buttons.length !== 1) {
      return { clicked: false, drawers: 1, buttons: buttons.length };
    }
    buttons[0].click();
    return { clicked: true, drawers: 1, buttons: 1 };
  })()`);
  if (!deleteOpened?.clicked) {
    throw new Error(
      `Expected one Memory details drawer and one Delete button: ` +
        JSON.stringify(deleteOpened),
    );
  }
  await waitFor(
    `[...document.querySelectorAll('[role="dialog"]')].filter(
      (item) => item.innerText.includes("Delete memory"),
    ).length === 1`,
    "memory delete confirmation",
  );
  const entered = await evaluate(`(() => {
    const dialogs = [...document.querySelectorAll('[role="dialog"]')].filter(
      (item) => item.innerText.includes("Delete memory"),
    );
    if (dialogs.length !== 1) return false;
    const input = dialogs[0].querySelector(
      'input[placeholder="Enter name to confirm"]',
    );
    if (!input) return false;
    const setter = Object.getOwnPropertyDescriptor(
      HTMLInputElement.prototype,
      "value",
    )?.set;
    setter.call(input, ${JSON.stringify(memoryId)});
    input.dispatchEvent(new Event("input", { bubbles: true }));
    return true;
  })()`);
  if (!entered) throw new Error("Exact-ID confirmation input was not found");
  await waitFor(
    `(() => {
      const dialogs = [...document.querySelectorAll('[role="dialog"]')].filter(
        (item) => item.innerText.includes("Delete memory"),
      );
      if (dialogs.length !== 1) return false;
      const buttons = [...dialogs[0].querySelectorAll("button")].filter(
        (item) => item.innerText.trim() === "Delete",
      );
      return buttons.length === 1 && !buttons[0].disabled;
    })()`,
    `exact confirmation ${memoryId}`,
  );
  const confirmed = await evaluate(`(() => {
    const dialogs = [...document.querySelectorAll('[role="dialog"]')].filter(
      (item) => item.innerText.includes("Delete memory"),
    );
    if (dialogs.length !== 1) return false;
    const buttons = [...dialogs[0].querySelectorAll("button")].filter(
      (item) => item.innerText.trim() === "Delete",
    );
    if (buttons.length !== 1 || buttons[0].disabled) return false;
    buttons[0].click();
    return true;
  })()`);
  if (!confirmed)
    throw new Error(`Exact-ID delete was not enabled for ${memoryId}`);
}

function observeExactDelete(cdp, memoryId) {
  const expectedPath = `/api/sidecar/v1/memories/${encodeURIComponent(memoryId)}`;
  const dashboardOrigin = new URL(dashboardBase).origin;
  const deleteRequests = new Set();
  let resolveDelete;
  let rejectDelete;
  let timeout;
  const responsePromise = new Promise((resolve, reject) => {
    resolveDelete = resolve;
    rejectDelete = reject;
    timeout = setTimeout(
      () =>
        reject(
          new Error(
            `No 2xx browser DELETE response observed for ${expectedPath}`,
          ),
        ),
      30000,
    );
  });

  cdp.on("Network.requestWillBeSent", ({ requestId, request }) => {
    const method = request?.method;
    const url = request?.url;
    if (method === "DELETE" && typeof url === "string") {
      const parsed = new URL(url);
      if (
        parsed.origin === dashboardOrigin &&
        parsed.pathname === expectedPath
      ) {
        deleteRequests.add(requestId);
      }
    }
  });
  cdp.on("Network.responseReceived", ({ requestId, response }) => {
    if (!deleteRequests.has(requestId)) return;
    const status = response?.status;
    if (status >= 200 && status < 300) {
      clearTimeout(timeout);
      resolveDelete({ requestId, status, url: response.url });
    } else {
      clearTimeout(timeout);
      rejectDelete(
        new Error(`Exact DELETE ${requestId} returned HTTP ${String(status)}`),
      );
    }
  });

  return {
    response: responsePromise,
    cancel(reason) {
      clearTimeout(timeout);
      rejectDelete(new Error(reason));
    },
  };
}

async function waitForMemoryToDisappear(cdp, memoryId, marker) {
  const { waitFor } = createBrowserDriver(cdp);
  const ariaLabel = `Open memory ${memoryId}`;
  await waitFor(
    `!document.querySelector(${JSON.stringify(`[aria-label="${ariaLabel}"]`)}) &&
      !document.body?.innerText?.includes(${JSON.stringify(marker)})`,
    `memory ${memoryId} to disappear from the UI`,
    30000,
  );
}

function scopedSidecarUrl(memoryId) {
  const query = new URLSearchParams({ project_id: projectId, app_id: appId });
  return `${sidecarBase}/v1/memories/${encodeURIComponent(memoryId)}?${query}`;
}

async function waitForDirectAbsence(label, url) {
  const deadline = Date.now() + 30000;
  let lastDiagnostic = "not checked";
  while (Date.now() < deadline) {
    const response = await fetchWithTimeout(url, { timeout: 5000 });
    if (response.status === 404) return;
    lastDiagnostic = await responseDiagnostic(response);
    if (response.status >= 500) throw new Error(`${label}: ${lastDiagnostic}`);
    await sleep(200);
  }
  throw new Error(`${label} still present: ${lastDiagnostic}`);
}

async function assertSidecarAbsent(memoryId) {
  await waitForDirectAbsence("direct sidecar GET", scopedSidecarUrl(memoryId));
}

async function classifyDirectMem0Get(response) {
  if (response.status === 404) return "absent";
  if (response.status !== 200) {
    throw new Error(
      `direct Mem0 GET failed: ${await responseDiagnostic(response)}`,
    );
  }
  const mediaType = (response.headers.get("content-type") || "")
    .split(";", 1)[0]
    .trim()
    .toLowerCase();
  if (mediaType !== "application/json" && !mediaType.endsWith("+json")) {
    throw new Error(
      `direct Mem0 GET returned non-JSON content type: ${mediaType}`,
    );
  }
  let payload;
  try {
    payload = await response.json();
  } catch (error) {
    throw new Error(
      `direct Mem0 GET returned invalid JSON: ${errorMessage(error)}`,
    );
  }
  return payload === null ? "absent" : "present";
}

async function assertMem0Absent(memoryId) {
  const url = `${mem0Base}/memories/${encodeURIComponent(memoryId)}`;
  const deadline = Date.now() + 30000;
  while (Date.now() < deadline) {
    const response = await fetchWithTimeout(url, { timeout: 5000 });
    const classification = await classifyDirectMem0Get(response);
    if (classification === "absent") return;
    await sleep(200);
  }
  throw new Error(`direct Mem0 GET still returned memory ${memoryId}`);
}

async function cleanupFixture(memoryId) {
  const failures = [];
  for (const [label, url] of [
    ["sidecar cleanup DELETE", scopedSidecarUrl(memoryId)],
    [
      "Mem0 cleanup DELETE",
      `${mem0Base}/memories/${encodeURIComponent(memoryId)}`,
    ],
  ]) {
    try {
      const response = await fetchWithTimeout(url, {
        method: "DELETE",
        timeout: 30000,
      });
      if (![200, 204, 404].includes(response.status)) {
        failures.push(`${label}: ${await responseDiagnostic(response)}`);
      }
    } catch (error) {
      failures.push(`${label}: ${errorMessage(error)}`);
    }
  }
  for (const [label, check] of [
    ["sidecar absence", () => assertSidecarAbsent(memoryId)],
    ["Mem0 absence", () => assertMem0Absent(memoryId)],
  ]) {
    try {
      await check();
    } catch (error) {
      failures.push(`${label}: ${errorMessage(error)}`);
    }
  }
  if (failures.length > 0) {
    throw new Error(`Fixture cleanup was not complete: ${failures.join("; ")}`);
  }
}

async function main() {
  let fixture;
  let cdp;
  const clientKeyLabel = `browser-client-${Date.now()}-${crypto.randomUUID()}`;
  let stage = "seed fixture through direct sidecar";
  let primaryError;
  try {
    stage = "create real Core dashboard session";
    const refreshToken = await dashboardRefreshToken();
    fixture = await seedFixtureThroughSidecar();
    stage = "connect to Chromium";
    await waitForBrowser();
    const target = await openTarget();
    cdp = new CdpSession(target.webSocketDebuggerUrl);
    await cdp.open();
    const pageErrors = [];
    cdp.on("Runtime.exceptionThrown", ({ exceptionDetails }) => {
      pageErrors.push(
        exceptionDetails?.exception?.description ||
          exceptionDetails?.text ||
          "Unknown browser exception",
      );
    });
    await cdp.send("Page.enable");
    await cdp.send("Runtime.enable");
    await cdp.send("Network.enable");
    await setViewport(cdp, {
      width: 1440,
      height: 900,
      mobile: false,
    });
    stage = "prove unauthenticated Client Keys redirect";
    await proveUnauthenticatedClientKeysRedirect(cdp);
    await waitForVisualStability(cdp, "unauthenticated login page");
    await captureBrowserEvidence(
      cdp,
      "client-keys-unauthenticated-desktop.png",
    );
    stage = "install dashboard session prerequisite";
    await setDashboardSessionPrerequisite(cdp, refreshToken);
    await setViewport(cdp, {
      width: 1440,
      height: 900,
      mobile: false,
    });

    stage = "create client key through live dashboard";
    const clientKey = await createClientKeyThroughDashboard(
      cdp,
      clientKeyLabel,
    );
    stage = "prove client key is visible only once";
    await assertClientKeyIsOneTimeOnly(
      cdp,
      clientKeyLabel,
      clientKey.descriptor,
      clientKey.secret,
    );
    await waitForVisualStability(cdp, "desktop Client Keys list");
    await captureBrowserEvidence(cdp, "client-keys-list-desktop.png");
    await setViewport(cdp, {
      width: 960,
      height: 1024,
      mobile: false,
    });
    await cdp.send("Page.navigate", {
      url: `${dashboardBase}/dashboard/api-keys`,
    });
    await createBrowserDriver(cdp).waitFor(
      `document.body?.innerText?.includes(${JSON.stringify(clientKeyLabel)}) === true`,
      "compact Client Keys list",
    );
    await waitForVisualStability(cdp, "compact Client Keys list");
    await assertClientKeysFitViewport(cdp);
    await captureBrowserEvidence(cdp, "client-keys-list-compact.png");
    await setViewport(cdp, {
      width: 1440,
      height: 900,
      mobile: false,
    });
    await cdp.send("Page.navigate", {
      url: `${dashboardBase}/dashboard/api-keys`,
    });
    await createBrowserDriver(cdp).waitFor(
      `document.body?.innerText?.includes(${JSON.stringify(clientKeyLabel)}) === true`,
      "restored desktop Client Keys list",
    );
    stage = "revoke client key through live dashboard";
    await revokeClientKeyThroughDashboard(cdp, clientKeyLabel);
    stage = "open live Next list and exact memory detail";
    await openMemoryDetails(cdp, fixture.memoryId, fixture.marker);
    stage = "perform exact-ID UI delete and observe matched 2xx response";
    const deleteObservation = observeExactDelete(cdp, fixture.memoryId);
    deleteObservation.response.catch(() => undefined);
    try {
      await confirmExactMemoryId(cdp, fixture.memoryId);
    } catch (error) {
      deleteObservation.cancel("Exact-ID confirmation failed before DELETE");
      await deleteObservation.response.catch(() => undefined);
      throw error;
    }
    const deleteResponse = await deleteObservation.response;
    stage = "prove memory disappears from browser UI";
    await waitForMemoryToDisappear(cdp, fixture.memoryId, fixture.marker);
    stage = "prove direct sidecar absence";
    await assertSidecarAbsent(fixture.memoryId);
    stage = "prove direct Mem0 absence";
    await assertMem0Absent(fixture.memoryId);
    if (pageErrors.length > 0) {
      throw new Error(`Browser exceptions: ${JSON.stringify(pageErrors)}`);
    }
    console.log(
      `Real destructive browser gate passed: memory=${fixture.memoryId} ` +
        `delete_request=${deleteResponse.requestId} status=${deleteResponse.status}`,
    );
  } catch (error) {
    primaryError = new Error(`stage=${stage}: ${errorMessage(error)}`);
  } finally {
    cdp?.close();
    if (fixture?.memoryId) {
      try {
        await cleanupFixture(fixture.memoryId);
      } catch (cleanupError) {
        const cleanupMessage = `stage=finally cleanup: ${errorMessage(cleanupError)}`;
        primaryError = primaryError
          ? new Error(`${primaryError.message}\n${cleanupMessage}`)
          : new Error(cleanupMessage);
      }
    }
    try {
      await cleanupClientKey(clientKeyLabel);
    } catch (cleanupError) {
      const cleanupMessage = `stage=finally client-key cleanup: ${errorMessage(cleanupError)}`;
      primaryError = primaryError
        ? new Error(`${primaryError.message}\n${cleanupMessage}`)
        : new Error(cleanupMessage);
    }
  }
  if (primaryError) throw primaryError;
}

module.exports = { classifyDirectMem0Get, setDashboardSessionPrerequisite };

if (require.main === module) {
  main().catch((error) => {
    console.error(errorMessage(error));
    process.exitCode = 1;
  });
}
