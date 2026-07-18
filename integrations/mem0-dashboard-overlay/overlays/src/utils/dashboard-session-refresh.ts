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
  ambiguityHistoryTtlMs?: number;
  ambiguousTokenGraceMs?: number;
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

interface CachedAmbiguousToken {
  kind: "ambiguous";
  expiresAt: number;
}

type CachedSession =
  | CachedAuthenticatedSession
  | CachedStaleToken
  | CachedAmbiguousToken;
type AuthenticatedDashboardSessionRefreshResult = Extract<
  DashboardSessionRefreshResult,
  { status: "authenticated" }
>;

interface RefreshCookieLease {
  active: boolean;
  requestTokenGuard: RefreshTokenGuard;
  resultTokenGuard: RefreshTokenGuard;
}

interface RefreshTokenGuard {
  invalidated: boolean;
  refreshToken: string;
  successors: Set<RefreshTokenGuard>;
}

const DEFAULT_MAX_CACHE_ENTRIES = 1024;
const DEFAULT_AMBIGUITY_HISTORY_TTL_MS = 5 * 60 * 1000;
const DEFAULT_AMBIGUOUS_TOKEN_GRACE_MS = 10 * 1000;
const DEFAULT_REFRESH_TIMEOUT_MS = 10 * 1000;
const DEFAULT_SESSION_CACHE_TTL_MS = 60 * 1000;
const DEFAULT_OLD_TOKEN_GRACE_MS = 10 * 1000;

export function createDashboardSessionRefreshCoordinator({
  refreshUpstream,
  now = Date.now,
  maxCacheEntries = DEFAULT_MAX_CACHE_ENTRIES,
  ambiguityHistoryTtlMs = DEFAULT_AMBIGUITY_HISTORY_TTL_MS,
  ambiguousTokenGraceMs = DEFAULT_AMBIGUOUS_TOKEN_GRACE_MS,
  oldTokenGraceMs = DEFAULT_OLD_TOKEN_GRACE_MS,
  refreshTimeoutMs = DEFAULT_REFRESH_TIMEOUT_MS,
  sessionCacheTtlMs = DEFAULT_SESSION_CACHE_TTL_MS,
}: DashboardSessionRefreshCoordinatorOptions) {
  const sessions = new Map<string, CachedSession>();
  const inFlight = new Map<string, Promise<DashboardSessionRefreshResult>>();
  const refreshCookieLeases = new Map<string, RefreshCookieLease>();
  const refreshTokenGuards = new Map<string, RefreshTokenGuard>();
  // Ambiguity history is deliberately per-token, bounded, and expiring. Once
  // a record is evicted or expires, a later upstream 401 is definitive again.
  const ambiguousTokens = new Map<string, number>();
  const resultRefreshCookieLeases = new WeakMap<
    AuthenticatedDashboardSessionRefreshResult,
    RefreshCookieLease
  >();
  const cacheLimit = Math.max(1, maxCacheEntries);
  const tokenGuardLimit = Math.max(2, cacheLimit * 2);

  function pruneExpired() {
    const currentTime = now();
    for (const [token, session] of sessions) {
      if (session.expiresAt <= currentTime) {
        sessions.delete(token);
      }
    }
    for (const [token, expiresAt] of ambiguousTokens) {
      if (expiresAt <= currentTime) {
        ambiguousTokens.delete(token);
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

  function getRefreshTokenGuard(refreshToken: string) {
    const existing = refreshTokenGuards.get(refreshToken);
    if (existing) {
      refreshTokenGuards.delete(refreshToken);
      refreshTokenGuards.set(refreshToken, existing);
      return existing;
    }
    const guard: RefreshTokenGuard = {
      invalidated: false,
      refreshToken,
      successors: new Set(),
    };
    refreshTokenGuards.set(refreshToken, guard);
    while (refreshTokenGuards.size > tokenGuardLimit) {
      const oldest = refreshTokenGuards.entries().next().value;
      if (!oldest) break;
      const [oldestToken, oldestGuard] = oldest;
      oldestGuard.invalidated = true;
      refreshTokenGuards.delete(oldestToken);
    }
    return guard;
  }

  function retireRefreshCookieLease(refreshToken: string) {
    const lease = refreshCookieLeases.get(refreshToken);
    if (lease) {
      lease.active = false;
      refreshCookieLeases.delete(refreshToken);
    }
  }

  function invalidateRefreshTokenGuard(
    guard: RefreshTokenGuard,
    visited = new Set<RefreshTokenGuard>(),
  ) {
    if (visited.has(guard)) return;
    visited.add(guard);
    guard.invalidated = true;
    sessions.delete(guard.refreshToken);
    ambiguousTokens.delete(guard.refreshToken);
    retireRefreshCookieLease(guard.refreshToken);
    for (const successor of guard.successors) {
      invalidateRefreshTokenGuard(successor, visited);
    }
  }

  function cacheRefreshCookieLease(
    refreshToken: string,
    lease: RefreshCookieLease,
  ) {
    const existing = refreshCookieLeases.get(refreshToken);
    if (existing && existing !== lease) {
      existing.active = false;
    }
    refreshCookieLeases.delete(refreshToken);
    refreshCookieLeases.set(refreshToken, lease);
    while (refreshCookieLeases.size > cacheLimit) {
      const oldest = refreshCookieLeases.entries().next().value;
      if (!oldest) break;
      const [oldestToken, oldestLease] = oldest;
      oldestLease.active = false;
      refreshCookieLeases.delete(oldestToken);
    }
  }

  function touchRefreshCookieLease(refreshToken: string) {
    const lease = refreshCookieLeases.get(refreshToken);
    if (!lease) return;
    refreshCookieLeases.delete(refreshToken);
    refreshCookieLeases.set(refreshToken, lease);
  }

  function quarantineAmbiguousToken(refreshToken: string) {
    ambiguousTokens.delete(refreshToken);
    ambiguousTokens.set(refreshToken, now() + ambiguityHistoryTtlMs);
    while (ambiguousTokens.size > cacheLimit) {
      const oldestToken = ambiguousTokens.keys().next().value;
      if (typeof oldestToken !== "string") break;
      ambiguousTokens.delete(oldestToken);
    }
    cache(refreshToken, {
      kind: "ambiguous",
      expiresAt: now() + ambiguousTokenGraceMs,
    });
  }

  function quarantineKnownAmbiguousToken(refreshToken: string) {
    const historyExpiresAt = ambiguousTokens.get(refreshToken);
    if (historyExpiresAt === undefined || historyExpiresAt <= now()) {
      ambiguousTokens.delete(refreshToken);
      return false;
    }
    ambiguousTokens.delete(refreshToken);
    ambiguousTokens.set(refreshToken, historyExpiresAt);
    cache(refreshToken, {
      kind: "ambiguous",
      expiresAt: Math.min(now() + ambiguousTokenGraceMs, historyExpiresAt),
    });
    return true;
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
    requestTokenGuard: RefreshTokenGuard,
  ): Promise<DashboardSessionRefreshResult> {
    let response: Response;
    try {
      response = await refreshWithTimeout(refreshToken);
    } catch {
      if (requestTokenGuard.invalidated) {
        return { status: "unauthorized" };
      }
      quarantineAmbiguousToken(refreshToken);
      return { status: "unavailable" };
    }
    if (requestTokenGuard.invalidated) {
      return { status: "unauthorized" };
    }
    if (!response.ok) {
      if (response.status === 401) {
        if (quarantineKnownAmbiguousToken(refreshToken)) {
          return { status: "unavailable" };
        }
        retireRefreshCookieLease(refreshToken);
        return { status: "unauthorized" };
      }
      return { status: "unavailable" };
    }

    const data = await response.json().catch(() => ({}));
    if (
      typeof data.access_token !== "string" ||
      typeof data.refresh_token !== "string"
    ) {
      quarantineAmbiguousToken(refreshToken);
      return { status: "unavailable" };
    }
    const result: AuthenticatedDashboardSessionRefreshResult = {
      status: "authenticated" as const,
      accessToken: data.access_token,
      refreshToken: data.refresh_token,
    };
    const resultTokenGuard = getRefreshTokenGuard(result.refreshToken);
    if (requestTokenGuard.invalidated || resultTokenGuard.invalidated) {
      return { status: "unauthorized" };
    }
    requestTokenGuard.successors.add(resultTokenGuard);
    ambiguousTokens.delete(refreshToken);
    retireRefreshCookieLease(refreshToken);
    const refreshCookieLease = {
      active: true,
      requestTokenGuard,
      resultTokenGuard,
    };
    resultRefreshCookieLeases.set(result, refreshCookieLease);
    cacheRefreshCookieLease(result.refreshToken, refreshCookieLease);
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
      const refreshTokenGuard = getRefreshTokenGuard(refreshToken);
      if (refreshTokenGuard.invalidated) {
        return { status: "unauthorized" };
      }
      const cached = sessions.get(refreshToken);
      if (cached?.kind === "authenticated") {
        sessions.delete(refreshToken);
        sessions.set(refreshToken, cached);
        touchRefreshCookieLease(refreshToken);
        return cached.result;
      }
      if (cached?.kind === "stale" || cached?.kind === "ambiguous") {
        return { status: "unavailable" };
      }

      const current = inFlight.get(refreshToken);
      if (current) {
        return current;
      }
      const pending = rotate(refreshToken, refreshTokenGuard).finally(() => {
        inFlight.delete(refreshToken);
      });
      inFlight.set(refreshToken, pending);
      return pending;
    },
    invalidateRefreshToken(refreshToken: string) {
      const guard = getRefreshTokenGuard(refreshToken);
      invalidateRefreshTokenGuard(guard);
    },
    shouldSetRefreshCookie(
      requestRefreshToken: string,
      result: DashboardSessionRefreshResult,
    ) {
      if (
        result.status !== "authenticated" ||
        result.refreshToken === requestRefreshToken
      ) {
        return false;
      }
      const lease = resultRefreshCookieLeases.get(result);
      return (
        lease?.active === true &&
        lease.requestTokenGuard.invalidated === false &&
        lease.resultTokenGuard.invalidated === false &&
        refreshCookieLeases.get(result.refreshToken) === lease
      );
    },
    getStats() {
      pruneExpired();
      return {
        ambiguousEntries: ambiguousTokens.size,
        cachedEntries: sessions.size,
        inFlight: inFlight.size,
        tokenGuards: refreshTokenGuards.size,
      };
    },
  };
}
