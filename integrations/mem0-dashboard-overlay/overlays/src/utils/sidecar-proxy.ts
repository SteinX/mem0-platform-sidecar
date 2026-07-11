const METHODS_WITH_BODY = new Set(["POST", "PUT", "PATCH"]);

type SidecarProxyOptions = {
  baseUrl: string | null;
  configuredProjectId: string;
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

function isAllowedSidecarRequest(method: string, path: string): boolean {
  return (
    isProjectCategoriesPath(method, path) ||
    isProjectCategoryItemPath(method, path) ||
    isExportPath(method, path)
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
  return path;
}

function scopedExportBody(
  bodyText: string,
  configuredProjectId: string,
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

  return JSON.stringify({
    ...payload,
    project_id: configuredProjectId,
  });
}

export async function proxySidecarRequest(
  request: Request,
  normalizedPath: string,
  options: SidecarProxyOptions,
): Promise<Response> {
  const { baseUrl, configuredProjectId, validateDashboardSession } = options;
  if (!baseUrl) {
    return jsonError("SIDECAR_INTERNAL_API_URL is not configured", 500);
  }
  const scopedPath = scopedSidecarPath(
    request.method,
    normalizedPath,
    configuredProjectId,
  );
  if (!scopedPath) {
    return jsonError("Sidecar route is not allowed", 403);
  }
  if (!(await validateDashboardSession())) {
    return jsonError("Unauthorized", 401);
  }

  const url = new URL(`${baseUrl}${scopedPath}`);
  new URL(request.url).searchParams.forEach((value, key) => {
    if (key !== "project_id") {
      url.searchParams.append(key, value);
    }
  });
  if (isExportPath(request.method, scopedPath)) {
    url.searchParams.set("project_id", configuredProjectId);
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
      const scopedBody = scopedExportBody(bodyText, configuredProjectId);
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
