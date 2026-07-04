import { NextRequest } from "next/server";

const METHODS_WITH_BODY = new Set(["POST", "PUT", "PATCH"]);

function getSidecarBaseUrl(): string {
  const baseUrl = process.env.SIDECAR_INTERNAL_API_URL;
  if (!baseUrl) {
    throw new Error("SIDECAR_INTERNAL_API_URL is required");
  }
  return baseUrl.replace(/\/$/, "");
}

async function proxy(
  request: NextRequest,
  context: { params: Promise<{ path: string[] }> },
) {
  const params = await context.params;
  const upstreamPath = params.path.join("/");
  const url = new URL(`${getSidecarBaseUrl()}/${upstreamPath}`);
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

  const response = await fetch(url, init);
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
