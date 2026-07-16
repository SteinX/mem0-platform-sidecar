#!/usr/bin/env node
"use strict";

const fs = require("node:fs");
const path = require("node:path");

const scriptPath = path.resolve(
  __dirname,
  "../../integrations/mem0-dashboard-overlay/scripts/run-browser-destructive-e2e.cjs",
);
const source = fs.readFileSync(scriptPath, "utf8");

function check(condition, message) {
  if (!condition) throw new Error(message);
}

async function rejects(action, label) {
  let rejected = false;
  try {
    await action();
  } catch {
    rejected = true;
  }
  check(rejected, `${label} must fail closed`);
}

async function main() {
  check(
    source.includes("if (require.main === module)"),
    "real browser script must be import-safe for executable helper contracts",
  );
  const { classifyDirectMem0Get } = require(scriptPath);
  check(
    typeof classifyDirectMem0Get === "function",
    "classifyDirectMem0Get must be exported",
  );

  check(
    (await classifyDirectMem0Get(new Response(null, { status: 404 }))) ===
      "absent",
    "HTTP 404 must classify as absent",
  );
  check(
    (await classifyDirectMem0Get(
      new Response("null", {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    )) === "absent",
    "HTTP 200 JSON null must classify as absent",
  );
  check(
    (await classifyDirectMem0Get(
      Response.json({ id: "still-present", memory: "real record" }),
    )) === "present",
    "HTTP 200 real memory must remain present",
  );
  await rejects(
    () =>
      classifyDirectMem0Get(
        new Response("{malformed", {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      ),
    "HTTP 200 malformed JSON",
  );
  await rejects(
    () =>
      classifyDirectMem0Get(
        new Response("null", {
          status: 200,
          headers: { "Content-Type": "text/plain" },
        }),
      ),
    "HTTP 200 non-JSON body",
  );
  await rejects(
    () => classifyDirectMem0Get(new Response("upstream failed", { status: 503 })),
    "HTTP 5xx",
  );

  console.log("browser destructive helper: direct Mem0 absence contract passed");
}

main().catch((error) => {
  console.error(error && error.stack ? error.stack : error);
  process.exitCode = 1;
});
