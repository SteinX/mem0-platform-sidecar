export type DashboardSessionRefreshResult =
  | {
      status: "authenticated";
      accessToken: string;
      refreshToken: string;
    }
  | { status: "unauthorized" }
  | { status: "unavailable" };

interface DashboardSessionRefreshCoordinatorOptions {
  refreshUpstream: (
    refreshToken: string,
    signal: AbortSignal,
  ) => Promise<Response>;
  now?: () => number;
  maxCacheEntries?: number;
  oldTokenGraceMs?: number;
  refreshTimeoutMs?: number;
  sessionCacheTtlMs?: number;
}

interface CachedAuthenticatedSession {
  kind: "authenticated";
  expiresAt: number;
  result: Extract<DashboardSessionRefreshResult, { status: "authenticated" }>;
}

interface CachedStaleToken {
  kind: "stale";
  expiresAt: number;
}

type CachedSession = CachedAuthenticatedSession | CachedStaleToken;

const DEFAULT_MAX_CACHE_ENTRIES = 1024;
const DEFAULT_REFRESH_TIMEOUT_MS = 10 * 1000;
const DEFAULT_SESSION_CACHE_TTL_MS = 60 * 1000;
const DEFAULT_OLD_TOKEN_GRACE_MS = 10 * 1000;

export function createDashboardSessionRefreshCoordinator({
  refreshUpstream,
  now = Date.now,
  maxCacheEntries = DEFAULT_MAX_CACHE_ENTRIES,
  oldTokenGraceMs = DEFAULT_OLD_TOKEN_GRACE_MS,
  refreshTimeoutMs = DEFAULT_REFRESH_TIMEOUT_MS,
  sessionCacheTtlMs = DEFAULT_SESSION_CACHE_TTL_MS,
}: DashboardSessionRefreshCoordinatorOptions) {
  const sessions = new Map<string, CachedSession>();
  const inFlight = new Map<string, Promise<DashboardSessionRefreshResult>>();
  const cacheLimit = Math.max(1, maxCacheEntries);

  function pruneExpired() {
    const currentTime = now();
    for (const [token, session] of sessions) {
      if (session.expiresAt <= currentTime) {
        sessions.delete(token);
      }
    }
  }

  function cache(refreshToken: string, session: CachedSession) {
    sessions.delete(refreshToken);
    sessions.set(refreshToken, session);
    while (sessions.size > cacheLimit) {
      const oldestToken = sessions.keys().next().value;
      if (typeof oldestToken !== "string") break;
      sessions.delete(oldestToken);
    }
  }

  async function refreshWithTimeout(refreshToken: string) {
    const controller = new AbortController();
    let timeoutId: ReturnType<typeof setTimeout> | undefined;
    const timeout = new Promise<never>((_, reject) => {
      timeoutId = setTimeout(() => {
        controller.abort();
        reject(new Error("Dashboard session refresh timed out"));
      }, refreshTimeoutMs);
    });

    try {
      return await Promise.race([
        refreshUpstream(refreshToken, controller.signal),
        timeout,
      ]);
    } finally {
      if (timeoutId !== undefined) clearTimeout(timeoutId);
    }
  }

  async function rotate(
    refreshToken: string,
  ): Promise<DashboardSessionRefreshResult> {
    let response: Response;
    try {
      response = await refreshWithTimeout(refreshToken);
    } catch {
      return { status: "unavailable" };
    }
    if (!response.ok) {
      return response.status === 401
        ? { status: "unauthorized" }
        : { status: "unavailable" };
    }

    const data = await response.json().catch(() => ({}));
    if (
      typeof data.access_token !== "string" ||
      typeof data.refresh_token !== "string"
    ) {
      return { status: "unavailable" };
    }
    const result = {
      status: "authenticated" as const,
      accessToken: data.access_token,
      refreshToken: data.refresh_token,
    };
    cache(refreshToken, {
      kind: "stale",
      expiresAt: now() + oldTokenGraceMs,
    });
    cache(result.refreshToken, {
      kind: "authenticated",
      expiresAt: now() + sessionCacheTtlMs,
      result,
    });
    return result;
  }

  return {
    async refresh(
      refreshToken: string,
    ): Promise<DashboardSessionRefreshResult> {
      pruneExpired();
      const cached = sessions.get(refreshToken);
      if (cached?.kind === "authenticated") {
        sessions.delete(refreshToken);
        sessions.set(refreshToken, cached);
        return cached.result;
      }
      if (cached?.kind === "stale") {
        return { status: "unavailable" };
      }

      const current = inFlight.get(refreshToken);
      if (current) {
        return current;
      }
      const pending = rotate(refreshToken).finally(() => {
        inFlight.delete(refreshToken);
      });
      inFlight.set(refreshToken, pending);
      return pending;
    },
    getStats() {
      pruneExpired();
      return {
        cachedEntries: sessions.size,
        inFlight: inFlight.size,
      };
    },
  };
}
