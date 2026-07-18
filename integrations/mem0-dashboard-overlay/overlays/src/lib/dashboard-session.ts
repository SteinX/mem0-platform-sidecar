import { AUTH_ENDPOINTS } from "@/utils/api-endpoints";
import { getServerApiUrl } from "@/lib/server-api-url";
import { createDashboardSessionRefreshCoordinator } from "@/utils/dashboard-session-refresh";

type DashboardSessionCoordinator = ReturnType<
  typeof createDashboardSessionRefreshCoordinator
>;

const globalSessionState = globalThis as typeof globalThis & {
  __mem0DashboardSessionCoordinator?: DashboardSessionCoordinator;
};

export const dashboardSessionRefreshCoordinator =
  globalSessionState.__mem0DashboardSessionCoordinator ??
  (globalSessionState.__mem0DashboardSessionCoordinator =
    createDashboardSessionRefreshCoordinator({
      refreshUpstream: (refreshToken, signal) =>
        fetch(`${getServerApiUrl()}${AUTH_ENDPOINTS.REFRESH}`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ refresh_token: refreshToken }),
          cache: "no-store",
          signal,
        }),
    }));
