export type DashboardSessionClientResult =
  | { status: "authenticated"; accessToken: string }
  | { status: "unauthorized" }
  | { status: "unavailable" };

type SessionFetch = (
  input: string | URL | Request,
  init?: RequestInit,
) => Promise<Response>;

export type DashboardSessionRetryAction = "ignore" | "refresh" | "logout";

export function dashboardSessionRetryAction(
  responseStatus: number | undefined,
  alreadyRetried: boolean,
): DashboardSessionRetryAction {
  if (responseStatus !== 401) return "ignore";
  return alreadyRetried ? "logout" : "refresh";
}

export function dashboardSessionRequestRetryAction(
  responseStatus: number | undefined,
  requestConfig: { __mem0AuthRetry?: boolean } | undefined,
): DashboardSessionRetryAction {
  const alreadyRetried = requestConfig
    ? requestConfig.__mem0AuthRetry === true
    : true;
  return dashboardSessionRetryAction(responseStatus, alreadyRetried);
}

export async function requestDashboardSessionRefresh(
  fetchSession: SessionFetch = fetch,
): Promise<DashboardSessionClientResult> {
  let response: Response;
  try {
    response = await fetchSession("/api/auth/refresh", {
      method: "POST",
      credentials: "include",
    });
  } catch {
    return { status: "unavailable" };
  }

  if (response.status === 401) {
    return { status: "unauthorized" };
  }
  if (!response.ok) {
    return { status: "unavailable" };
  }

  const data = await response.json().catch(() => ({}));
  if (typeof data.access_token !== "string") {
    return { status: "unavailable" };
  }
  return { status: "authenticated", accessToken: data.access_token };
}
