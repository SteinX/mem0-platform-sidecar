"use client";

import {
  createContext,
  useCallback,
  useEffect,
  useMemo,
  useState,
} from "react";
import { api, setAccessToken } from "@/utils/api";
import { AUTH_ENDPOINTS } from "@/utils/api-endpoints";
import {
  DashboardSessionClientResult,
  requestDashboardSessionRefresh,
} from "@/utils/dashboard-session-client";

export interface AuthUser {
  id: string;
  name: string;
  email: string;
  role: string;
  created_at: string;
}

interface AuthContextValue {
  user: AuthUser | null;
  isLoading: boolean;
  isAdmin: boolean;
  login: (email: string, password: string) => Promise<void>;
  register: (name: string, email: string, password: string) => Promise<void>;
  logout: () => Promise<void>;
  refreshUser: () => Promise<void>;
}

export const AuthContext = createContext<AuthContextValue>({
  user: null,
  isLoading: true,
  isAdmin: false,
  login: async () => {},
  register: async () => {},
  logout: async () => {},
  refreshUser: async () => {},
});

const INITIAL_RETRY_DELAY_MS = 1000;
const MAX_RETRY_DELAY_MS = 30_000;

async function storeRefreshToken(refreshToken: string) {
  await fetch("/api/auth/refresh", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ refresh_token: refreshToken }),
  });
}

async function clearRefreshToken() {
  await fetch("/api/auth/refresh", { method: "DELETE" });
}

async function refreshSession(): Promise<DashboardSessionClientResult> {
  const result = await requestDashboardSessionRefresh();
  if (result.status === "authenticated") {
    setAccessToken(result.accessToken);
  }
  return result;
}

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [user, setUser] = useState<AuthUser | null>(null);
  const [isLoading, setIsLoading] = useState(true);

  const loadUser = useCallback(async () => {
    const res = await api.get<AuthUser>(AUTH_ENDPOINTS.ME);
    setUser(res.data);
  }, []);

  useEffect(() => {
    let active = true;
    let retryTimer: ReturnType<typeof setTimeout> | undefined;
    let retryDelay = INITIAL_RETRY_DELAY_MS;

    const scheduleRetry = () => {
      if (!active) return;
      retryTimer = setTimeout(() => {
        void restoreSession();
      }, retryDelay);
      retryDelay = Math.min(retryDelay * 2, MAX_RETRY_DELAY_MS);
    };

    const restoreSession = async () => {
      const result = await refreshSession();
      if (!active) return;

      if (result.status === "unauthorized") {
        setAccessToken(null);
        setUser(null);
        setIsLoading(false);
        return;
      }
      if (result.status === "unavailable") {
        scheduleRetry();
        return;
      }

      try {
        await loadUser();
        if (active) setIsLoading(false);
      } catch {
        scheduleRetry();
      }
    };

    void restoreSession();
    return () => {
      active = false;
      if (retryTimer !== undefined) clearTimeout(retryTimer);
    };
  }, [loadUser]);

  const login = useCallback(
    async (email: string, password: string) => {
      const res = await api.post(AUTH_ENDPOINTS.LOGIN, { email, password });
      setAccessToken(res.data.access_token);
      await storeRefreshToken(res.data.refresh_token);
      await loadUser();
    },
    [loadUser],
  );

  const register = useCallback(
    async (name: string, email: string, password: string) => {
      const res = await api.post(AUTH_ENDPOINTS.REGISTER, {
        name,
        email,
        password,
      });
      setAccessToken(res.data.access_token);
      await storeRefreshToken(res.data.refresh_token);
      await loadUser();
    },
    [loadUser],
  );

  const logout = useCallback(async () => {
    await clearRefreshToken();
    setAccessToken(null);
    setUser(null);
    if (typeof window !== "undefined") window.location.href = "/login";
  }, []);

  const value = useMemo<AuthContextValue>(
    () => ({
      user,
      isLoading,
      isAdmin: user?.role === "admin",
      login,
      register,
      logout,
      refreshUser: loadUser,
    }),
    [user, isLoading, login, register, logout, loadUser],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}
