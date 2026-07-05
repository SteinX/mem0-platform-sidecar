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
): Promise<T> {
  const response = await fetch(withParams(path, params), { method: "GET" });
  return parseResponse<T>(response);
}

export async function sidecarPut<T>(path: string, body: unknown): Promise<T> {
  const response = await fetch(`${SIDECAR_API_PREFIX}${normalizeSidecarPath(path)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return parseResponse<T>(response);
}

export async function sidecarPost<T>(path: string, body: unknown): Promise<T> {
  const response = await fetch(`${SIDECAR_API_PREFIX}${normalizeSidecarPath(path)}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return parseResponse<T>(response);
}
