export type CategoryScalarType = "string" | "number" | "boolean" | "date" | "enum";
export type CategoryFieldType = CategoryScalarType | "array" | "object";

export type CategoryField = {
  id: string;
  key: string;
  title: string;
  description: string;
  type: CategoryFieldType;
  required: boolean;
  hasDefault: boolean;
  defaultValue: string;
  enumValues: string[];
  arrayItemType: CategoryScalarType;
  arrayEnumValues: string[];
  children: CategoryField[];
};

export type CategorySchemaEditor = {
  mode: "builder" | "advanced";
  fields: CategoryField[];
  rawSchemaText: string;
  unsupportedPaths: string[];
};

export type CategorySchemaValidation = {
  valid: boolean;
  fieldErrors: Record<string, string>;
  formError: string | null;
};

type SchemaObject = Record<string, unknown>;

const ROOT_KEYS = new Set(["type", "properties", "required"]);
const FIELD_KEYS = new Set(["type", "title", "description", "default"]);
const SCALAR_KEYS = new Set([...FIELD_KEYS, "format", "enum"]);
const ARRAY_KEYS = new Set([...FIELD_KEYS, "items"]);
const OBJECT_KEYS = new Set([...FIELD_KEYS, "properties", "required"]);
const ARRAY_ITEM_KEYS = new Set(["type", "format", "enum"]);
const SCALAR_TYPES = new Set(["string", "number", "boolean"]);

export function createEmptyField(): CategoryField {
  return {
    id: crypto.randomUUID(),
    key: "",
    title: "",
    description: "",
    type: "string",
    required: false,
    hasDefault: false,
    defaultValue: "",
    enumValues: [],
    arrayItemType: "string",
    arrayEnumValues: [],
    children: [],
  };
}

export function setFieldDefaultEnabled(
  field: CategoryField,
  hasDefault: boolean,
): CategoryField {
  return {
    ...field,
    hasDefault,
    defaultValue:
      hasDefault && field.type === "boolean" && !field.defaultValue
        ? "false"
        : field.defaultValue,
  };
}

export function setFieldType(
  field: CategoryField,
  type: CategoryFieldType,
): CategoryField {
  return setFieldDefaultEnabled({ ...field, type }, field.hasDefault);
}

export function schemaToEditor(schema: unknown): CategorySchemaEditor {
  const rawSchemaText = JSON.stringify(schema, null, 2) ?? "";
  const unsupportedPaths: string[] = [];

  if (!isSchemaObject(schema)) {
    return advancedEditor(rawSchemaText, ["$"]);
  }

  if (Object.keys(schema).length === 0) {
    return {
      mode: "builder",
      fields: [],
      rawSchemaText,
      unsupportedPaths,
    };
  }

  addUnsupportedKeys(schema, ROOT_KEYS, "$", unsupportedPaths);
  if (schema.type !== "object") {
    addUnsupportedPath(unsupportedPaths, "$.type");
  }

  const rootRequired = parseRequired(schema.required, "$.required", unsupportedPaths);
  const properties = schema.properties;
  if (properties !== undefined && !isSchemaObject(properties)) {
    addUnsupportedPath(unsupportedPaths, "$.properties");
  }

  const fields: CategoryField[] = [];
  const propertyKeys = isSchemaObject(properties)
    ? new Set(Object.keys(properties))
    : new Set<string>();
  validateRequiredKeys(rootRequired, propertyKeys, "$.required", unsupportedPaths);
  if (isSchemaObject(properties)) {
    for (const [key, property] of Object.entries(properties)) {
      const field = schemaPropertyToField(
        property,
        key,
        rootRequired.includes(key),
        propertyPath("$.properties", key),
        unsupportedPaths,
        0,
      );
      if (field !== null) {
        fields.push(field);
      }
    }
  }

  if (unsupportedPaths.length > 0) {
    return advancedEditor(rawSchemaText, unsupportedPaths);
  }

  return {
    mode: "builder",
    fields,
    rawSchemaText,
    unsupportedPaths,
  };
}

export function editorToSchema(fields: CategoryField[]): SchemaObject {
  const properties: SchemaObject = {};
  const required: string[] = [];

  for (const field of fields) {
    Object.defineProperty(properties, field.key, {
      value: fieldToSchema(field),
      enumerable: true,
      configurable: true,
      writable: true,
    });
    if (field.required) {
      required.push(field.key);
    }
  }

  const schema: SchemaObject = { type: "object", properties };
  if (required.length > 0) {
    schema.required = required;
  }
  return schema;
}

export function validateCategoryFields(fields: CategoryField[]): CategorySchemaValidation {
  const fieldErrors: Record<string, string> = {};
  validateFields(fields, fieldErrors, false);

  return {
    valid: Object.keys(fieldErrors).length === 0,
    fieldErrors,
    formError: null,
  };
}

export function countSchemaFields(schema: unknown): number {
  if (!isSchemaObject(schema)) {
    return 0;
  }
  return countProperties(schema.properties);
}

function advancedEditor(
  rawSchemaText: string,
  unsupportedPaths: string[],
): CategorySchemaEditor {
  return {
    mode: "advanced",
    fields: [],
    rawSchemaText,
    unsupportedPaths,
  };
}

function schemaPropertyToField(
  value: unknown,
  key: string,
  required: boolean,
  path: string,
  unsupportedPaths: string[],
  depth: number,
): CategoryField | null {
  if (!isSchemaObject(value)) {
    addUnsupportedPath(unsupportedPaths, path);
    return null;
  }

  const type = value.type;
  if (typeof type !== "string") {
    addUnsupportedPath(unsupportedPaths, `${path}.type`);
    return null;
  }

  validateFieldText(value, path, unsupportedPaths);

  const base = createSchemaField(key, value, required);
  if (SCALAR_TYPES.has(type)) {
    addUnsupportedKeys(value, SCALAR_KEYS, path, unsupportedPaths);
    const field = scalarFieldFromSchema(base, value, path, unsupportedPaths);
    validateSchemaDefault(field, value, path, unsupportedPaths);
    return field;
  }

  if (type === "array") {
    addUnsupportedKeys(value, ARRAY_KEYS, path, unsupportedPaths);
    const items = value.items;
    if (!isSchemaObject(items)) {
      addUnsupportedPath(unsupportedPaths, `${path}.items`);
      return null;
    }
    base.type = "array";
    base.arrayItemType = arrayItemTypeFromSchema(items, `${path}.items`, unsupportedPaths);
    base.arrayEnumValues = enumValuesFromSchema(
      items,
      `${path}.items.enum`,
      unsupportedPaths,
    );
    validateSchemaDefault(base, value, path, unsupportedPaths);
    return base;
  }

  if (type === "object") {
    addUnsupportedKeys(value, OBJECT_KEYS, path, unsupportedPaths);
    if (depth > 0) {
      addUnsupportedPath(unsupportedPaths, path);
      return null;
    }
    const properties = value.properties;
    if (properties !== undefined && !isSchemaObject(properties)) {
      addUnsupportedPath(unsupportedPaths, `${path}.properties`);
      return null;
    }
    const childRequired = parseRequired(
      value.required,
      `${path}.required`,
      unsupportedPaths,
    );
    const propertyKeys = new Set(Object.keys(properties ?? {}));
    validateRequiredKeys(
      childRequired,
      propertyKeys,
      `${path}.required`,
      unsupportedPaths,
    );
    base.type = "object";
    if (isSchemaObject(properties)) {
      for (const [childKey, childValue] of Object.entries(properties)) {
        const child = schemaPropertyToField(
          childValue,
          childKey,
          childRequired.includes(childKey),
          propertyPath(`${path}.properties`, childKey),
          unsupportedPaths,
          depth + 1,
        );
        if (child !== null) {
          base.children.push(child);
        }
      }
    }
    validateSchemaDefault(base, value, path, unsupportedPaths);
    return base;
  }

  addUnsupportedPath(unsupportedPaths, `${path}.type`);
  return null;
}

function createSchemaField(key: string, schema: SchemaObject, required: boolean): CategoryField {
  const field = createEmptyField();
  field.key = key;
  field.title = stringValue(schema.title);
  field.description = stringValue(schema.description);
  field.required = required;
  field.hasDefault = hasOwn(schema, "default");
  field.defaultValue = defaultValueToEditor(schema.default);
  return field;
}

function scalarFieldFromSchema(
  field: CategoryField,
  schema: SchemaObject,
  path: string,
  unsupportedPaths: string[],
): CategoryField {
  const format = schema.format;
  if (format !== undefined && format !== "date") {
    addUnsupportedPath(unsupportedPaths, `${path}.format`);
  }

  const enumValues = enumValuesFromSchema(schema, `${path}.enum`, unsupportedPaths);
  if (schema.enum !== undefined) {
    if (schema.type !== "string") {
      addUnsupportedPath(unsupportedPaths, `${path}.enum`);
    }
    if (format !== undefined) {
      addUnsupportedPath(unsupportedPaths, `${path}.format`);
    }
    field.type = "enum";
    field.enumValues = enumValues;
  } else if (format === "date") {
    if (schema.type !== "string") {
      addUnsupportedPath(unsupportedPaths, `${path}.format`);
    }
    field.type = "date";
  } else {
    field.type = schema.type as "string" | "number" | "boolean";
  }
  return field;
}

function arrayItemTypeFromSchema(
  items: SchemaObject,
  path: string,
  unsupportedPaths: string[],
): CategoryScalarType {
  addUnsupportedKeys(items, ARRAY_ITEM_KEYS, path, unsupportedPaths);
  if (!SCALAR_TYPES.has(items.type as string)) {
    addUnsupportedPath(unsupportedPaths, `${path}.type`);
    return "string";
  }

  const format = items.format;
  if (format !== undefined && format !== "date") {
    addUnsupportedPath(unsupportedPaths, `${path}.format`);
  }
  if (items.enum !== undefined) {
    if (items.type !== "string") {
      addUnsupportedPath(unsupportedPaths, `${path}.enum`);
    }
    if (format !== undefined) {
      addUnsupportedPath(unsupportedPaths, `${path}.format`);
    }
    return "enum";
  }
  if (format === "date") {
    if (items.type !== "string") {
      addUnsupportedPath(unsupportedPaths, `${path}.format`);
    }
    return "date";
  }
  return items.type as "string" | "number" | "boolean";
}

function enumValuesFromSchema(
  schema: SchemaObject,
  path: string,
  unsupportedPaths: string[],
): string[] {
  if (schema.enum === undefined) {
    return [];
  }
  if (!Array.isArray(schema.enum) || schema.enum.some((value) => typeof value !== "string")) {
    addUnsupportedPath(unsupportedPaths, path);
    return [];
  }
  return schema.enum;
}

function fieldToSchema(field: CategoryField): SchemaObject {
  const schema: SchemaObject = {};
  if (field.title.trim()) {
    schema.title = field.title;
  }
  if (field.description.trim()) {
    schema.description = field.description;
  }

  if (field.type === "object") {
    const childSchema = editorToSchema(field.children);
    schema.type = "object";
    schema.properties = childSchema.properties;
    if (childSchema.required !== undefined) {
      schema.required = childSchema.required;
    }
  } else if (field.type === "array") {
    schema.type = "array";
    schema.items = scalarSchema(field.arrayItemType, field.arrayEnumValues);
  } else {
    Object.assign(schema, scalarSchema(field.type, field.enumValues));
  }

  if (field.hasDefault) {
    const parsedDefault = parseDefaultValue(field);
    if (parsedDefault !== undefined) {
      schema.default = parsedDefault;
    }
  }
  return schema;
}

function scalarSchema(type: CategoryScalarType, enumValues: string[]): SchemaObject {
  if (type === "date") {
    return { type: "string", format: "date" };
  }
  if (type === "enum") {
    return { type: "string", enum: enumValues.filter((value) => value.trim()) };
  }
  return { type };
}

function parseDefaultValue(field: CategoryField): unknown {
  if (field.type === "number") {
    return Number(field.defaultValue);
  }
  if (field.type === "boolean") {
    return field.defaultValue.trim() === "true";
  }
  if (field.type === "array" || field.type === "object") {
    try {
      return JSON.parse(field.defaultValue);
    } catch {
      return undefined;
    }
  }
  return field.defaultValue;
}

function validateFields(
  fields: CategoryField[],
  fieldErrors: Record<string, string>,
  nested: boolean,
): void {
  const keyCounts = new Map<string, number>();
  for (const field of fields) {
    const key = field.key.trim();
    if (key) {
      keyCounts.set(key, (keyCounts.get(key) ?? 0) + 1);
    }
  }

  for (const field of fields) {
    const key = field.key.trim();
    if (!key) {
      setFieldError(fieldErrors, field.id, "Field key is required");
    } else if ((keyCounts.get(key) ?? 0) > 1) {
      setFieldError(fieldErrors, field.id, "Field keys must be unique");
    }

    if (field.type === "object") {
      if (nested) {
        setFieldError(fieldErrors, field.id, "Nested objects are not supported");
      }
      validateFields(field.children, fieldErrors, true);
    }

    if ((field as { arrayItemType?: string }).arrayItemType === "object") {
      setFieldError(fieldErrors, field.id, "Arrays of objects are not supported");
    }

    validateEnumValues(field, fieldErrors);
    validateDefaultValue(field, fieldErrors);
  }
}

function validateEnumValues(
  field: CategoryField,
  fieldErrors: Record<string, string>,
): void {
  const values =
    field.type === "enum"
      ? field.enumValues
      : field.type === "array" && field.arrayItemType === "enum"
        ? field.arrayEnumValues
        : null;
  if (values === null) {
    return;
  }

  const nonEmptyValues = values.map((value) => value.trim()).filter(Boolean);
  if (nonEmptyValues.length === 0) {
    setFieldError(fieldErrors, field.id, "Add at least one enum option");
  } else if (new Set(nonEmptyValues).size !== nonEmptyValues.length) {
    setFieldError(fieldErrors, field.id, "Enum options must be unique");
  }
}

function validateDefaultValue(
  field: CategoryField,
  fieldErrors: Record<string, string>,
): void {
  if (!field.hasDefault) {
    return;
  }
  const value = field.defaultValue.trim();
  if (field.type === "number" && (!value || !Number.isFinite(Number(value)))) {
    setFieldError(fieldErrors, field.id, "Default must be a number");
  } else if (field.type === "boolean" && value !== "true" && value !== "false") {
    setFieldError(fieldErrors, field.id, "Default must be true or false");
  } else if (field.type === "date" && !isIsoDate(value)) {
    setFieldError(fieldErrors, field.id, "Default must use YYYY-MM-DD");
  } else if (field.type === "enum" && !field.enumValues.includes(field.defaultValue)) {
    setFieldError(fieldErrors, field.id, "Default must be one of the enum options");
  } else if (field.type === "array") {
    const arrayDefault = parseJsonArray(value);
    if (arrayDefault === null) {
      setFieldError(fieldErrors, field.id, "Default must be a JSON array");
    } else if (
      !arrayDefault.every((item) =>
        isScalarDefaultCompatible(
          field.arrayItemType,
          item,
          field.arrayEnumValues,
        ),
      )
    ) {
      setFieldError(
        fieldErrors,
        field.id,
        "Default array items must match the item type",
      );
    }
  } else if (field.type === "object" && !isJsonObject(value)) {
    setFieldError(fieldErrors, field.id, "Default must be a JSON object");
  }
}

function validateSchemaDefault(
  field: CategoryField,
  schema: SchemaObject,
  path: string,
  unsupportedPaths: string[],
): void {
  if (field.hasDefault && !isCategoryDefaultCompatible(field, schema.default)) {
    addUnsupportedPath(unsupportedPaths, `${path}.default`);
  }
}

function isCategoryDefaultCompatible(field: CategoryField, value: unknown): boolean {
  if (field.type === "array") {
    return (
      Array.isArray(value) &&
      value.every((item) =>
        isScalarDefaultCompatible(
          field.arrayItemType,
          item,
          field.arrayEnumValues,
        ),
      )
    );
  }
  if (field.type === "object") {
    return isSchemaObject(value);
  }
  return isScalarDefaultCompatible(field.type, value, field.enumValues);
}

function isScalarDefaultCompatible(
  type: CategoryScalarType,
  value: unknown,
  enumValues: string[],
): boolean {
  if (type === "string") {
    return typeof value === "string";
  }
  if (type === "number") {
    return typeof value === "number" && Number.isFinite(value);
  }
  if (type === "boolean") {
    return typeof value === "boolean";
  }
  if (type === "date") {
    return typeof value === "string" && isIsoDate(value);
  }
  return typeof value === "string" && enumValues.includes(value);
}

function isIsoDate(value: string): boolean {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) {
    return false;
  }
  const [year, month, day] = value.split("-").map(Number);
  const date = new Date(Date.UTC(year, month - 1, day));
  return (
    date.getUTCFullYear() === year &&
    date.getUTCMonth() === month - 1 &&
    date.getUTCDate() === day
  );
}

function parseJsonArray(value: string): unknown[] | null {
  try {
    const parsed = JSON.parse(value);
    return Array.isArray(parsed) ? parsed : null;
  } catch {
    return null;
  }
}

function isJsonObject(value: string): boolean {
  try {
    const parsed = JSON.parse(value);
    return isSchemaObject(parsed);
  } catch {
    return false;
  }
}

function parseRequired(
  required: unknown,
  path: string,
  unsupportedPaths: string[],
): string[] {
  if (required === undefined) {
    return [];
  }
  if (!Array.isArray(required) || required.some((key) => typeof key !== "string")) {
    addUnsupportedPath(unsupportedPaths, path);
    return [];
  }
  if (new Set(required).size !== required.length) {
    addUnsupportedPath(unsupportedPaths, path);
  }
  return required;
}

function validateFieldText(
  schema: SchemaObject,
  path: string,
  unsupportedPaths: string[],
): void {
  for (const key of ["title", "description"] as const) {
    if (schema[key] !== undefined && typeof schema[key] !== "string") {
      addUnsupportedPath(unsupportedPaths, `${path}.${key}`);
    }
  }
}

function validateRequiredKeys(
  required: string[],
  propertyKeys: Set<string>,
  path: string,
  unsupportedPaths: string[],
): void {
  if (required.some((key) => !propertyKeys.has(key))) {
    addUnsupportedPath(unsupportedPaths, path);
  }
}

function addUnsupportedKeys(
  schema: SchemaObject,
  allowedKeys: Set<string>,
  path: string,
  unsupportedPaths: string[],
): void {
  for (const key of Object.keys(schema)) {
    if (!allowedKeys.has(key)) {
      addUnsupportedPath(unsupportedPaths, propertyPath(path, key));
    }
  }
}

function addUnsupportedPath(paths: string[], path: string): void {
  if (!paths.includes(path)) {
    paths.push(path);
  }
}

function countProperties(properties: unknown): number {
  if (!isSchemaObject(properties)) {
    return 0;
  }
  return Object.values(properties).reduce<number>(
    (count, property) =>
      count + 1 + (isSchemaObject(property) ? countProperties(property.properties) : 0),
    0,
  );
}

function defaultValueToEditor(value: unknown): string {
  if (value === undefined) {
    return "";
  }
  if (typeof value === "string") {
    return value;
  }
  return JSON.stringify(value);
}

function hasOwn(schema: SchemaObject, key: string): boolean {
  return Object.prototype.hasOwnProperty.call(schema, key);
}

function stringValue(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function isSchemaObject(value: unknown): value is SchemaObject {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function propertyPath(path: string, key: string): string {
  return /^[A-Za-z_$][A-Za-z0-9_$]*$/.test(key)
    ? `${path}.${key}`
    : `${path}[${JSON.stringify(key)}]`;
}

function setFieldError(
  fieldErrors: Record<string, string>,
  fieldId: string,
  message: string,
): void {
  if (!fieldErrors[fieldId]) {
    fieldErrors[fieldId] = message;
  }
}
