let cachedProjectId: string | null = null;

export async function getSidecarProjectId(): Promise<string> {
  if (cachedProjectId) {
    return cachedProjectId;
  }

  const response = await fetch("/api/sidecar/config", {
    method: "GET",
    cache: "no-store",
  });
  const data = (await response.json().catch(() => ({}))) as {
    project_id?: unknown;
  };
  if (
    !response.ok ||
    typeof data.project_id !== "string" ||
    !data.project_id.trim()
  ) {
    throw new Error("Failed to resolve sidecar project");
  }

  cachedProjectId = data.project_id.trim();
  return cachedProjectId;
}
