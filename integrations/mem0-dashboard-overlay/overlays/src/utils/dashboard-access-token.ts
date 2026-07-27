function decodeBase64Url(value: string): string {
  const normalized = value.replaceAll("-", "+").replaceAll("_", "/");
  const paddingLength = (4 - (normalized.length % 4)) % 4;
  return atob(normalized.padEnd(normalized.length + paddingLength, "="));
}

export function isAdminDashboardAccessToken(accessToken: string): boolean {
  const segments = accessToken.split(".");
  const payloadSegment = segments[1];
  if (
    segments.length !== 3 ||
    payloadSegment === undefined ||
    !/^[A-Za-z0-9_-]+$/.test(payloadSegment)
  ) {
    return false;
  }

  try {
    const payload: unknown = JSON.parse(decodeBase64Url(payloadSegment));
    return (
      typeof payload === "object" &&
      payload !== null &&
      "role" in payload &&
      payload.role === "admin"
    );
  } catch (error) {
    if (error instanceof SyntaxError || error instanceof DOMException) {
      return false;
    }
    throw error;
  }
}
