import axios, { AxiosError, AxiosInstance } from "axios";
import {
  dashboardSessionRequestRetryAction,
  dashboardSessionRetryAction,
  DashboardSessionClientResult,
  requestDashboardSessionRefresh,
} from "@/utils/dashboard-session-client";

type RetryableAxiosConfig = NonNullable<AxiosError["config"]> & {
  __mem0AuthRetry?: boolean;
};

let cachedToken: string | null = null;
const LOGIN_PATH = "/login";

export const setAccessToken = (token: string | null) => {
  cachedToken = token;
};

export const getAccessToken = (): string | null => {
  return cachedToken;
};

const handleTokenError = () => {
  cachedToken = null;
};

const redirectToLogin = () => {
  if (typeof window !== "undefined") {
    window.location.href = LOGIN_PATH;
  }
};

const refreshAccessToken = async (): Promise<DashboardSessionClientResult> => {
  const result = await requestDashboardSessionRefresh();
  if (result.status === "authenticated") {
    setAccessToken(result.accessToken);
  }
  return result;
};

const createApi = (): AxiosInstance & {
  postStream: (url: string, data: unknown) => Promise<Response>;
} => {
  const api = axios.create({
    baseURL: process.env.NEXT_PUBLIC_API_URL,
  });

  api.interceptors.request.use(
    async (config) => {
      if (cachedToken) {
        config.headers = config.headers ?? {};
        config.headers.Authorization = `Bearer ${cachedToken}`;
      }
      return config;
    },
    (error) => {
      return Promise.reject(error);
    },
  );

  api.interceptors.response.use(
    (response) => response,
    async (error: AxiosError<{ error?: string }>) => {
      const requestConfig = error.config as RetryableAxiosConfig | undefined;
      const retryAction = dashboardSessionRequestRetryAction(
        error.response?.status,
        requestConfig,
      );
      if (retryAction === "logout") {
        handleTokenError();
        redirectToLogin();
        return Promise.reject(error);
      }
      if (retryAction === "refresh" && requestConfig) {
        handleTokenError();
        const result = await refreshAccessToken();

        if (result.status === "authenticated") {
          requestConfig.__mem0AuthRetry = true;
          requestConfig.headers = requestConfig.headers ?? {};
          requestConfig.headers.Authorization = `Bearer ${result.accessToken}`;
          return api.request(requestConfig);
        }
        if (result.status === "unauthorized") {
          handleTokenError();
          redirectToLogin();
        }
        if (result.status === "unavailable") {
          return Promise.reject(error);
        }
        return Promise.reject(error);
      }

      if (error.response?.data?.error) {
        return Promise.reject(error.response.data.error);
      }

      return Promise.reject(error);
    },
  );

  const postStream = async (url: string, data: unknown): Promise<Response> => {
    const send = () =>
      fetch(`${process.env.NEXT_PUBLIC_API_URL}${url}`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: cachedToken ? `Bearer ${cachedToken}` : "",
        },
        body: JSON.stringify(data),
      });

    let response = await send();
    if (response.status === 401) {
      handleTokenError();
      const result = await refreshAccessToken();
      if (result.status === "authenticated") {
        response = await send();
      } else if (result.status === "unauthorized") {
        redirectToLogin();
        throw new Error("Unauthorized");
      } else {
        throw new Error("Authentication temporarily unavailable");
      }
    }
    if (dashboardSessionRetryAction(response.status, true) === "logout") {
      handleTokenError();
      redirectToLogin();
      throw new Error("Unauthorized");
    }

    if (!response.ok) {
      const errorData = await response.json().catch(() => ({}));
      throw new Error(errorData.error || "Request failed");
    }

    return response;
  };

  return Object.assign(api, { postStream });
};

export const api = createApi();
