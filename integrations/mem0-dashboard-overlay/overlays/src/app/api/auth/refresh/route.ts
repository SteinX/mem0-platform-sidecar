import { cookies } from "next/headers";
import { NextRequest, NextResponse } from "next/server";
import { dashboardSessionRefreshCoordinator } from "@/lib/dashboard-session";

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

export async function POST() {
  const cookieStore = await cookies();
  const refreshToken = cookieStore.get(COOKIE_NAME)?.value;

  if (!refreshToken) {
    return NextResponse.json({ error: "No refresh token" }, { status: 401 });
  }

  const result = await dashboardSessionRefreshCoordinator.refresh(refreshToken);
  if (result.status === "unauthorized") {
    cookieStore.delete(COOKIE_NAME);
    return NextResponse.json({ error: "Refresh failed" }, { status: 401 });
  }
  if (result.status === "unavailable") {
    return NextResponse.json(
      { error: "Authentication service temporarily unavailable" },
      { status: 503 },
    );
  }

  if (
    dashboardSessionRefreshCoordinator.shouldSetRefreshCookie(
      refreshToken,
      result,
    )
  ) {
    cookieStore.set(COOKIE_NAME, result.refreshToken, COOKIE_OPTIONS);
  }
  return NextResponse.json({ access_token: result.accessToken });
}

export async function PUT(request: NextRequest) {
  const body = await request.json();
  const cookieStore = await cookies();

  if (!body.refresh_token) {
    return NextResponse.json(
      { error: "Missing refresh_token" },
      { status: 400 },
    );
  }

  const previousRefreshToken = cookieStore.get(COOKIE_NAME)?.value;
  if (previousRefreshToken && previousRefreshToken !== body.refresh_token) {
    dashboardSessionRefreshCoordinator.invalidateRefreshToken(
      previousRefreshToken,
    );
  }
  cookieStore.set(COOKIE_NAME, body.refresh_token, COOKIE_OPTIONS);
  return NextResponse.json({ ok: true });
}

export async function DELETE() {
  const cookieStore = await cookies();
  const refreshToken = cookieStore.get(COOKIE_NAME)?.value;
  if (refreshToken) {
    dashboardSessionRefreshCoordinator.invalidateRefreshToken(refreshToken);
  }
  cookieStore.delete(COOKIE_NAME);
  return NextResponse.json({ ok: true });
}
