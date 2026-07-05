import { cookies } from "next/headers";
import { NextRequest } from "next/server";
import { AUTH_ENDPOINTS } from "@/utils/api-endpoints";
import { getServerApiUrl } from "@/lib/server-api-url";

const METHODS_WITH_BODY = new Set(["POST", "PUT"]);
const COOKIE_NAME = "mem0_refresh_token";

function shouldUseSecureCookie() {
  const dashboardUrl = process.env.DASHBOARD_URL;
  if (!dashboardUrl) {
    return process.env.NODE_ENV === "production";
  }

  try {
    return new URL(dashboardUrl).protocol === "https:";
  } catch {
    return process.env.NODE_ENV === "production";
  }
}

const COOKIE_OPTIONS = {
  httpOnly: true,
  secure: shouldUseSecureCookie(),
  sameSite: "lax" as const,
  path: "/",
  maxAge: 30 * 24 * 60 * 60,
};

function getSidecarBaseUrl(): string | null {
  const baseUrl = process.env.SIDECAR_INTERNAL_API_URL;
  if (!baseUrl) {
    return null;
  }
  return baseUrl.replace(/\/$/, "");
}

function jsonError(message: string, status: number): Response {
  return Response.json({ error: message }, { status });
}

function isProjectCategoriesPath(method: string, path: string): boolean {
  return (
    (method === "GET" || method === "PUT") &&
    /^\/v1\/projects\/[^/]+\/categories$/.test(path)
  );
}

function isExportPath(method: string, path: string): boolean {
  if ((method === "GET" || method === "POST") && path === "/v1/exports") {
    return true;
  }
  return method === "GET" && /^\/v1\/exports\/[^/]+\/download$/.test(path);
}

function isAllowedSidecarRequest(method: string, path: string): boolean {
  return isProjectCategoriesPath(method, path) || isExportPath(method, path);
}

async function validateDashboardSession(): Promise<boolean> {
  const cookieStore = await cookies();
  const refreshToken = cookieStore.get(COOKIE_NAME)?.value;
  if (!refreshToken) {
    return false;
  }

  try {
    const response = await fetch(`${getServerApiUrl()}${AUTH_ENDPOINTS.REFRESH}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ refresh_token: refreshToken }),
      cache: "no-store",
    });

    if (!response.ok) {
      cookieStore.delete(COOKIE_NAME);
      return false;
    }

    const data = await response.json().catch(() => ({}));
    if (typeof data.refresh_token === "string") {
      cookieStore.set(COOKIE_NAME, data.refresh_token, COOKIE_OPTIONS);
    }
    return typeof data.access_token === "string";
  } catch {
    return false;
  }
}

async function proxy(
  request: NextRequest,
  context: { params: Promise<{ path: string[] }> },
) {
  const params = await context.params;
  const upstreamPath = params.path.join("/");
  const normalizedPath = `/${upstreamPath}`;
  const baseUrl = getSidecarBaseUrl();
  if (!baseUrl) {
    return jsonError("SIDECAR_INTERNAL_API_URL is not configured", 500);
  }
  if (!isAllowedSidecarRequest(request.method, normalizedPath)) {
    return jsonError("Sidecar route is not allowed", 403);
  }
  if (!(await validateDashboardSession())) {
    return jsonError("Unauthorized", 401);
  }

  const url = new URL(`${baseUrl}/${upstreamPath}`);
  request.nextUrl.searchParams.forEach((value, key) => {
    url.searchParams.append(key, value);
  });

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
    init.body = await request.text();
  }

  let response: Response;
  try {
    response = await fetch(url, init);
  } catch {
    return jsonError("Sidecar upstream request failed", 502);
  }

  const responseText = await response.text();
  return new Response(responseText, {
    status: response.status,
    headers: {
      "Content-Type": response.headers.get("Content-Type") ?? "application/json",
    },
  });
}

export const GET = proxy;
export const POST = proxy;
export const PUT = proxy;
