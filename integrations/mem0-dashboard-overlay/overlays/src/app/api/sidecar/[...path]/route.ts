import { NextRequest } from "next/server";

const METHODS_WITH_BODY = new Set(["POST", "PUT"]);

function getSidecarBaseUrl(): string | null {
  const baseUrl = process.env.SIDECAR_INTERNAL_API_URL;
  if (!baseUrl) {
    return null;
  }
  return baseUrl.replace(/\/$/, "");
}

function jsonError(message: string, status: number): Response {
  return Response.json({ error: message }, { status });
}

async function proxy(
  request: NextRequest,
  context: { params: Promise<{ path: string[] }> },
) {
  const params = await context.params;
  const upstreamPath = params.path.join("/");
  const baseUrl = getSidecarBaseUrl();
  if (!baseUrl) {
    return jsonError("SIDECAR_INTERNAL_API_URL is not configured", 500);
  }

  const url = new URL(`${baseUrl}/${upstreamPath}`);
  request.nextUrl.searchParams.forEach((value, key) => {
    url.searchParams.append(key, value);
  });

  const headers = new Headers();
  headers.set("Content-Type", "application/json");
  const requestId = request.headers.get("X-Request-ID");
  if (requestId) {
    headers.set("X-Request-ID", requestId);
  }

  const init: RequestInit = {
    method: request.method,
    headers,
  };

  if (METHODS_WITH_BODY.has(request.method)) {
    init.body = await request.text();
  }

  let response: Response;
  try {
    response = await fetch(url, init);
  } catch {
    return jsonError("Sidecar upstream request failed", 502);
  }

  const responseText = await response.text();
  return new Response(responseText, {
    status: response.status,
    headers: {
      "Content-Type": response.headers.get("Content-Type") ?? "application/json",
    },
  });
}

export const GET = proxy;
export const POST = proxy;
export const PUT = proxy;
export const DELETE = proxy;
