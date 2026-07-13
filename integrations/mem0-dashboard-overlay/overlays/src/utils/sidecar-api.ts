const SIDECAR_API_PREFIX = "/api/sidecar";

function normalizeSidecarPath(path: string): string {
  return path.startsWith("/") ? path : `/${path}`;
}

async function parseResponse<T>(response: Response): Promise<T> {
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const message =
      typeof data.detail === "string"
        ? data.detail
        : typeof data.error === "string"
          ? data.error
          : "Sidecar request failed";
    throw new Error(message);
  }
  return data as T;
}

function withParams(path: string, params: Record<string, string> = {}): string {
  const search = new URLSearchParams(params);
  const query = search.toString();
  const normalizedPath = normalizeSidecarPath(path);
  return query
    ? `${SIDECAR_API_PREFIX}${normalizedPath}?${query}`
    : `${SIDECAR_API_PREFIX}${normalizedPath}`;
}

export async function sidecarGet<T>(
  path: string,
  params?: Record<string, string>,
  options: Pick<RequestInit, "signal"> = {},
): Promise<T> {
  const response = await fetch(withParams(path, params), {
    method: "GET",
    signal: options.signal,
  });
  return parseResponse<T>(response);
}

async function sidecarRequest<T>(
  method: "POST" | "PUT" | "PATCH",
  path: string,
  body: unknown,
  options: Pick<RequestInit, "signal"> = {},
): Promise<T> {
  const response = await fetch(
    `${SIDECAR_API_PREFIX}${normalizeSidecarPath(path)}`,
    {
      method,
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal: options.signal,
    },
  );
  return parseResponse<T>(response);
}

export async function sidecarPut<T>(path: string, body: unknown): Promise<T> {
  return sidecarRequest<T>("PUT", path, body);
}

export async function sidecarPost<T>(path: string, body: unknown): Promise<T> {
  return sidecarRequest<T>("POST", path, body);
}

export async function sidecarQuery<T>(
  path: string,
  body: object,
  options: Pick<RequestInit, "signal"> = {},
): Promise<T> {
  return sidecarRequest<T>("POST", path, body, options);
}

export async function sidecarPatch<T>(path: string, body: object): Promise<T> {
  return sidecarRequest<T>("PATCH", path, body);
}

export async function sidecarDelete(path: string): Promise<void> {
  const response = await fetch(
    `${SIDECAR_API_PREFIX}${normalizeSidecarPath(path)}`,
    {
      method: "DELETE",
    },
  );
  if (!response.ok) {
    await parseResponse<never>(response);
  }
}
