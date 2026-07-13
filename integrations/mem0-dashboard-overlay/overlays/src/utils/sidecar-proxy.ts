const METHODS_WITH_BODY = new Set(["POST", "PUT", "PATCH"]);

type SidecarProxyOptions = {
  baseUrl: string | null;
  configuredProjectId: string;
  configuredAppId?: string;
  validateDashboardSession: () => Promise<boolean>;
  fetchUpstream?: typeof fetch;
};

function jsonError(message: string, status: number): Response {
  return Response.json({ error: message }, { status });
}

function isProjectCategoriesPath(method: string, path: string): boolean {
  return (
    (method === "GET" || method === "POST" || method === "PUT") &&
    /^\/v1\/projects\/[^/]+\/categories$/.test(path)
  );
}

function isProjectCategoryItemPath(method: string, path: string): boolean {
  return (
    (method === "PATCH" || method === "DELETE") &&
    /^\/v1\/projects\/[^/]+\/categories\/[^/]+$/.test(path)
  );
}

function isExportPath(method: string, path: string): boolean {
  if ((method === "GET" || method === "POST") && path === "/v1/exports") {
    return true;
  }
  return method === "GET" && /^\/v1\/exports\/[^/]+\/download$/.test(path);
}

function isMemoryQueryPath(method: string, path: string): boolean {
  return method === "POST" && path === "/v1/memories/query";
}

function canonicalResourceId(
  encodedId: string,
  reservedIds: ReadonlySet<string>,
): string | null {
  if (!encodedId) {
    return null;
  }

  let resourceId: string;
  try {
    resourceId = decodeURIComponent(encodedId);
  } catch {
    return null;
  }

  const hasTraversalSegment = resourceId
    .split(/[\\/]/)
    .some((segment) => segment === "." || segment === "..");
  if (
    reservedIds.has(resourceId) ||
    hasTraversalSegment ||
    /[\u0000-\u001f\u007f]/.test(resourceId) ||
    resourceId.includes("%")
  ) {
    return null;
  }
  return encodeURIComponent(encodeURIComponent(resourceId));
}

function canonicalMemoryId(encodedId: string): string | null {
  return canonicalResourceId(encodedId, new Set(["query"]));
}

function memoryItemId(path: string): string | null {
  const match = path.match(/^\/v1\/memories\/([^/]+)$/);
  return match && match[1] !== "query" ? canonicalMemoryId(match[1]) : null;
}

function memoryHistoryId(path: string): string | null {
  const match = path.match(/^\/v1\/memories\/([^/]+)\/history$/);
  return match ? canonicalMemoryId(match[1]) : null;
}

function isMemoryItemPath(method: string, path: string): boolean {
  return (
    (method === "GET" || method === "PATCH" || method === "DELETE") &&
    memoryItemId(path) !== null
  );
}

function isMemoryHistoryPath(method: string, path: string): boolean {
  return method === "GET" && memoryHistoryId(path) !== null;
}

function isMemoryPath(method: string, path: string): boolean {
  return (
    isMemoryQueryPath(method, path) ||
    isMemoryItemPath(method, path) ||
    isMemoryHistoryPath(method, path)
  );
}

function isEventQueryPath(method: string, path: string): boolean {
  return method === "POST" && path === "/v1/events/query";
}

function eventItemId(path: string): string | null {
  const match = path.match(/^\/v1\/event\/([^/]+)$/);
  return match
    ? canonicalResourceId(match[1], new Set(["query"]))
    : null;
}

function isEventItemPath(method: string, path: string): boolean {
  return method === "GET" && eventItemId(path) !== null;
}

function isEventPath(method: string, path: string): boolean {
  return isEventQueryPath(method, path) || isEventItemPath(method, path);
}

function sidecarPathFromRequestUrl(
  request: Request,
  normalizedPath: string,
): string {
  if (
    !normalizedPath.startsWith("/v1/memories/") &&
    !normalizedPath.startsWith("/v1/event/")
  ) {
    return normalizedPath;
  }

  const pathname = new URL(request.url).pathname;
  const proxyPrefix = "/api/sidecar";
  const prefixIndex = pathname.lastIndexOf(proxyPrefix);
  if (prefixIndex === -1) {
    return normalizedPath;
  }
  const requestPath = pathname.slice(prefixIndex + proxyPrefix.length);
  return requestPath.startsWith("/v1/memories/") ||
    requestPath.startsWith("/v1/event/")
    ? requestPath
    : normalizedPath;
}

function isAllowedSidecarRequest(method: string, path: string): boolean {
  return (
    isProjectCategoriesPath(method, path) ||
    isProjectCategoryItemPath(method, path) ||
    isExportPath(method, path) ||
    isMemoryPath(method, path) ||
    isEventPath(method, path)
  );
}

function scopedSidecarPath(
  method: string,
  path: string,
  configuredProjectId: string,
): string | null {
  if (!isAllowedSidecarRequest(method, path)) {
    return null;
  }
  if (isProjectCategoriesPath(method, path)) {
    return `/v1/projects/${encodeURIComponent(configuredProjectId)}/categories`;
  }
  const categoryItemMatch = path.match(
    /^\/v1\/projects\/[^/]+\/categories\/([^/]+)$/,
  );
  if (categoryItemMatch) {
    const categoryId = categoryItemMatch[1];
    return `/v1/projects/${encodeURIComponent(configuredProjectId)}/categories/${encodeURIComponent(categoryId)}`;
  }
  if (isMemoryQueryPath(method, path)) {
    return path;
  }
  if (isEventQueryPath(method, path)) {
    return path;
  }
  const eventId = eventItemId(path);
  if (eventId !== null) {
    return `/v1/event/${eventId}`;
  }
  const historyId = memoryHistoryId(path);
  if (historyId !== null) {
    return `/v1/memories/${historyId}/history`;
  }
  const itemId = memoryItemId(path);
  if (itemId !== null) {
    return `/v1/memories/${itemId}`;
  }
  return path;
}

function isPortableScopeId(value: unknown, maximum: number): value is string {
  return (
    typeof value === "string" &&
    value.length > 0 &&
    value.length <= maximum &&
    value === value.trim() &&
    value.normalize("NFC") === value &&
    !/\s/u.test(value) &&
    !/\p{C}/u.test(value)
  );
}

function hasConfiguredTraceScope(
  configuredProjectId: string,
  configuredAppId?: string,
): configuredAppId is string {
  return (
    isPortableScopeId(configuredProjectId, 128) &&
    isPortableScopeId(configuredAppId, 256)
  );
}

function scopedJsonBody(
  bodyText: string,
  configuredProjectId: string,
  configuredAppId?: string,
): string | Response {
  const payloadText = bodyText.trim() || "{}";
  let payload: unknown;
  try {
    payload = JSON.parse(payloadText);
  } catch {
    return jsonError("Invalid JSON body", 400);
  }

  if (
    typeof payload !== "object" ||
    payload === null ||
    Array.isArray(payload)
  ) {
    return jsonError("Invalid JSON body", 400);
  }

  const scopedPayload: Record<string, unknown> = { ...payload };
  delete scopedPayload.project_id;
  delete scopedPayload.app_id;
  scopedPayload.project_id = configuredProjectId;
  if (configuredAppId !== undefined) {
    scopedPayload.app_id = configuredAppId;
  }
  return JSON.stringify(scopedPayload);
}

export async function proxySidecarRequest(
  request: Request,
  normalizedPath: string,
  options: SidecarProxyOptions,
): Promise<Response> {
  const {
    baseUrl,
    configuredProjectId,
    configuredAppId,
    validateDashboardSession,
  } = options;
  if (!baseUrl) {
    return jsonError("SIDECAR_INTERNAL_API_URL is not configured", 500);
  }
  const requestPath = sidecarPathFromRequestUrl(request, normalizedPath);
  const scopedPath = scopedSidecarPath(
    request.method,
    requestPath,
    configuredProjectId,
  );
  if (!scopedPath) {
    return jsonError("Sidecar route is not allowed", 403);
  }
  if (!(await validateDashboardSession())) {
    return jsonError("Unauthorized", 401);
  }

  const isEventRequest = isEventPath(request.method, requestPath);
  if (
    isEventRequest &&
    !hasConfiguredTraceScope(configuredProjectId, configuredAppId)
  ) {
    return jsonError("Sidecar trace scope is not configured", 500);
  }
  const isMemoryItemRequest = isMemoryItemPath(request.method, requestPath);
  const isMemoryHistoryRequest = isMemoryHistoryPath(
    request.method,
    requestPath,
  );
  const isMemoryRequest = isMemoryPath(request.method, requestPath);
  const url = new URL(`${baseUrl}${scopedPath}`);
  if (!isMemoryRequest && !isEventRequest) {
    new URL(request.url).searchParams.forEach((value, key) => {
      if (key !== "project_id" && key !== "app_id") {
        url.searchParams.append(key, value);
      }
    });
  }
  if (isExportPath(request.method, scopedPath)) {
    url.searchParams.set("project_id", configuredProjectId);
  }
  if (
    isMemoryItemRequest ||
    isMemoryHistoryRequest
  ) {
    url.searchParams.set("project_id", configuredProjectId);
    if (configuredAppId !== undefined) {
      url.searchParams.set("app_id", configuredAppId);
    }
  }
  if (isEventItemPath(request.method, requestPath)) {
    url.searchParams.set("project_id", configuredProjectId);
    url.searchParams.set("app_id", configuredAppId!);
  }

  const headers = new Headers();
  headers.set("Content-Type", "application/json");
  const requestId = request.headers.get("X-Request-ID");
  if (requestId) {
    headers.set("X-Request-ID", requestId);
  }

  const init: RequestInit = {
    method: request.method,
    headers,
  };

  if (METHODS_WITH_BODY.has(request.method)) {
    const bodyText = await request.text();
    if (request.method === "POST" && scopedPath === "/v1/exports") {
      const scopedBody = scopedJsonBody(bodyText, configuredProjectId);
      if (scopedBody instanceof Response) {
        return scopedBody;
      }
      init.body = scopedBody;
    } else if (
      isMemoryQueryPath(request.method, scopedPath) ||
      isEventQueryPath(request.method, scopedPath) ||
      isMemoryItemRequest
    ) {
      const scopedBody = scopedJsonBody(
        bodyText,
        configuredProjectId,
        configuredAppId,
      );
      if (scopedBody instanceof Response) {
        return scopedBody;
      }
      init.body = scopedBody;
    } else {
      init.body = bodyText;
    }
  }

  let response: Response;
  try {
    response = await (options.fetchUpstream ?? fetch)(url, init);
  } catch {
    return jsonError("Sidecar upstream request failed", 502);
  }

  const responseBody = response.body === null ? null : await response.text();
  return new Response(responseBody, {
    status: response.status,
    headers: {
      "Content-Type":
        response.headers.get("Content-Type") ?? "application/json",
    },
  });
}
