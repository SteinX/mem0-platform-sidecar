#!/usr/bin/env node
"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { webcrypto } = require("node:crypto");
const { createRequire } = require("node:module");

function loadCategorySchemaModule(dashboardDir) {
  if (!globalThis.crypto) {
    globalThis.crypto = webcrypto;
  }

  const dashboardRequire = createRequire(
    path.join(dashboardDir, "package.json"),
  );
  const typescript = dashboardRequire("typescript");
  const schemaPath = path.join(dashboardDir, "src/utils/category-schema.ts");
  const source = fs.readFileSync(schemaPath, "utf8");
  const transpiled = typescript.transpileModule(source, {
    compilerOptions: {
      module: typescript.ModuleKind.CommonJS,
      target: typescript.ScriptTarget.ES2022,
    },
    fileName: schemaPath,
    reportDiagnostics: true,
  });
  const errors = (transpiled.diagnostics || []).filter(
    (diagnostic) => diagnostic.category === typescript.DiagnosticCategory.Error,
  );
  assert.deepEqual(errors, [], "category schema transpilation failed");

  const module = { exports: {} };
  new Function("exports", "module", "require", transpiled.outputText)(
    module.exports,
    module,
    dashboardRequire,
  );
  return module.exports;
}

function emptyField(schema, key, type) {
  return { ...schema.createEmptyField(), key, type };
}

function testSupportedRoundTripAndEmptyStringDefault(schema) {
  const input = {
    type: "object",
    properties: {
      title: { type: "string", default: "" },
      score: { type: "number", default: 4 },
      active: { type: "boolean", default: false },
      due: { type: "string", format: "date", default: "2026-07-11" },
      status: { type: "string", enum: ["open", "closed"], default: "open" },
      tags: {
        type: "array",
        items: { type: "string", enum: ["one", "two"] },
        default: ["one"],
      },
      profile: {
        type: "object",
        properties: { owner: { type: "string", default: "Ada" } },
        required: ["owner"],
      },
    },
    required: ["title"],
  };

  const editor = schema.schemaToEditor(input);
  assert.equal(editor.mode, "builder");
  assert.equal(editor.fields[0].hasDefault, true);
  assert.equal(editor.fields[0].defaultValue, "");
  assert.deepEqual(schema.editorToSchema(editor.fields), input);
}

function testTypeInconsistentDefaultsUseAdvancedMode(schema) {
  const inputs = [
    { type: "number", default: "4" },
    { type: "boolean", default: 0 },
    { type: "string", default: 4 },
    { type: "array", items: { type: "string" }, default: [4] },
  ];

  for (const property of inputs) {
    const input = { type: "object", properties: { value: property } };
    assert.equal(schema.schemaToEditor(input).mode, "advanced");
  }
}

function testArrayDefaultElementsFollowItemType(schema) {
  const invalidDate = emptyField(schema, "dates", "array");
  invalidDate.hasDefault = true;
  invalidDate.defaultValue = '["2026-02-30"]';
  invalidDate.arrayItemType = "date";

  const invalidEnum = emptyField(schema, "states", "array");
  invalidEnum.hasDefault = true;
  invalidEnum.defaultValue = '["unknown"]';
  invalidEnum.arrayItemType = "enum";
  invalidEnum.arrayEnumValues = ["open", "closed"];

  const validation = schema.validateCategoryFields([invalidDate, invalidEnum]);
  assert.equal(
    validation.fieldErrors[invalidDate.id],
    "Default array items must match the item type",
  );
  assert.equal(
    validation.fieldErrors[invalidEnum.id],
    "Default array items must match the item type",
  );
}

function testDirectEnumDefaultMustMatchOption(schema) {
  const field = emptyField(schema, "status", "enum");
  field.enumValues = ["open", "closed"];
  field.hasDefault = true;
  field.defaultValue = "unknown";

  const validation = schema.validateCategoryFields([field]);
  assert.equal(
    validation.fieldErrors[field.id],
    "Default must be one of the enum options",
  );
}

function testDuplicateKeysMarkEverySibling(schema) {
  const first = emptyField(schema, "duplicate", "string");
  const second = emptyField(schema, "duplicate", "number");
  const validation = schema.validateCategoryFields([first, second]);

  assert.equal(validation.fieldErrors[first.id], "Field keys must be unique");
  assert.equal(validation.fieldErrors[second.id], "Field keys must be unique");
}

function testChildScopesRemainIndependent(schema) {
  const root = emptyField(schema, "shared", "string");
  const left = emptyField(schema, "left", "object");
  left.children = [emptyField(schema, "shared", "string")];
  const right = emptyField(schema, "right", "object");
  right.children = [emptyField(schema, "shared", "string")];

  assert.equal(schema.validateCategoryFields([root, left, right]).valid, true);
}

function testFieldKeysAreTrimmedDuringSerialization(schema) {
  const root = emptyField(schema, " user_id ", "string");
  root.required = true;
  const child = emptyField(schema, " display_name ", "string");
  child.required = true;
  const profile = emptyField(schema, " profile ", "object");
  profile.children = [child];

  const result = schema.editorToSchema([root, profile]);

  assert.deepEqual(Object.keys(result.properties), ["user_id", "profile"]);
  assert.deepEqual(result.required, ["user_id"]);
  assert.deepEqual(Object.keys(result.properties.profile.properties), [
    "display_name",
  ]);
  assert.deepEqual(result.properties.profile.required, ["display_name"]);
}

function testMalformedRequiredUsesAdvancedMode(schema) {
  const duplicate = schema.schemaToEditor({
    type: "object",
    properties: { value: { type: "string" } },
    required: ["value", "value"],
  });
  const unknown = schema.schemaToEditor({
    type: "object",
    properties: { value: { type: "string" } },
    required: ["missing"],
  });

  assert.equal(duplicate.mode, "advanced");
  assert.equal(unknown.mode, "advanced");
  assert.ok(duplicate.unsupportedPaths.includes("$.required"));
  assert.ok(unknown.unsupportedPaths.includes("$.required"));
}

function testAdvancedSchemasStillCountProperties(schema) {
  const input = {
    type: "object",
    additionalProperties: false,
    properties: {
      profile: {
        type: "object",
        properties: { owner: { type: "string" } },
      },
      status: { type: "string" },
    },
  };

  assert.equal(schema.schemaToEditor(input).mode, "advanced");
  assert.equal(schema.countSchemaFields(input), 3);
}

function testUnsupportedPathsUseBracketNotation(schema) {
  const editor = schema.schemaToEditor({
    type: "object",
    properties: {
      "field-name": { type: "string", minLength: 1 },
    },
  });

  assert.equal(editor.mode, "advanced");
  assert.ok(
    editor.unsupportedPaths.includes('$.properties["field-name"].minLength'),
  );
}

function assertRequiredOwnPropertySurvivesJson(schemaObject, key) {
  assert.equal(Object.hasOwn(schemaObject.properties, key), true);
  assert.equal(
    Object.prototype.propertyIsEnumerable.call(schemaObject.properties, key),
    true,
  );
  assert.ok(schemaObject.required.includes(key));

  const parsed = JSON.parse(JSON.stringify(schemaObject));
  assert.equal(Object.hasOwn(parsed.properties, key), true);
  assert.deepEqual(parsed.properties[key], { type: "string" });
  assert.ok(parsed.required.includes(key));
}

function testProtoKeySurvivesAtRoot(schema) {
  const field = emptyField(schema, "__proto__", "string");
  field.required = true;

  assertRequiredOwnPropertySurvivesJson(
    schema.editorToSchema([field]),
    "__proto__",
  );
}

function testProtoKeySurvivesInObjectChild(schema) {
  const child = emptyField(schema, "__proto__", "string");
  child.required = true;
  const parent = emptyField(schema, "profile", "object");
  parent.children = [child];

  const result = schema.editorToSchema([parent]);
  assertRequiredOwnPropertySurvivesJson(result.properties.profile, "__proto__");
}

function main() {
  if (process.argv.length !== 3) {
    throw new Error("usage: test-category-schema.cjs <dashboard-dir>");
  }

  const schema = loadCategorySchemaModule(path.resolve(process.argv[2]));
  testSupportedRoundTripAndEmptyStringDefault(schema);
  testTypeInconsistentDefaultsUseAdvancedMode(schema);
  testArrayDefaultElementsFollowItemType(schema);
  testDirectEnumDefaultMustMatchOption(schema);
  testDuplicateKeysMarkEverySibling(schema);
  testChildScopesRemainIndependent(schema);
  testFieldKeysAreTrimmedDuringSerialization(schema);
  testMalformedRequiredUsesAdvancedMode(schema);
  testAdvancedSchemasStillCountProperties(schema);
  testUnsupportedPathsUseBracketNotation(schema);
  testProtoKeySurvivesAtRoot(schema);
  testProtoKeySurvivesInObjectChild(schema);
  console.log("category schema harness: 12 contracts passed");
}

try {
  main();
} catch (error) {
  console.error(error);
  process.exitCode = 1;
}
