import { cookies } from "next/headers";
import { NextRequest } from "next/server";
import { dashboardSessionRefreshCoordinator } from "@/lib/dashboard-session";
import { proxySidecarRequest } from "@/utils/sidecar-proxy";

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

function getConfiguredProjectId(): string {
  return (
    process.env.SIDECAR_PROJECT_ID?.trim() ||
    process.env.MEM0_SIDECAR_DEFAULT_PROJECT_ID?.trim() ||
    "default"
  );
}

function getConfiguredAppId(): string | undefined {
  return process.env.SIDECAR_APP_ID?.trim() || undefined;
}

function isAuthDisabled() {
  const value = process.env.AUTH_DISABLED?.toLowerCase();
  return value === "1" || value === "true" || value === "yes" || value === "on";
}

async function validateDashboardSession(): Promise<boolean> {
  if (isAuthDisabled()) {
    return true;
  }

  const cookieStore = await cookies();
  const refreshToken = cookieStore.get(COOKIE_NAME)?.value;
  if (!refreshToken) {
    return false;
  }

  const result = await dashboardSessionRefreshCoordinator.refresh(refreshToken);
  if (result.status === "unauthorized") {
    cookieStore.delete(COOKIE_NAME);
    return false;
  }
  if (result.status === "unavailable") {
    throw new DashboardSessionUnavailableError();
  }

  cookieStore.set(COOKIE_NAME, result.refreshToken, COOKIE_OPTIONS);
  return true;
}

class DashboardSessionUnavailableError extends Error {}

async function proxy(
  request: NextRequest,
  context: { params: Promise<{ path: string[] }> },
) {
  const params = await context.params;
  const upstreamPath = params.path.join("/");
  const normalizedPath = `/${upstreamPath}`;
  try {
    return await proxySidecarRequest(request, normalizedPath, {
      baseUrl: getSidecarBaseUrl(),
      configuredProjectId: getConfiguredProjectId(),
      configuredAppId: getConfiguredAppId(),
      validateDashboardSession,
      fetchUpstream: fetch,
    });
  } catch (error) {
    if (error instanceof DashboardSessionUnavailableError) {
      return Response.json(
        { error: "Authentication service temporarily unavailable" },
        { status: 503 },
      );
    }
    throw error;
  }
}

export const GET = proxy;
export const POST = proxy;
export const PUT = proxy;
export const PATCH = proxy;
export const DELETE = proxy;
