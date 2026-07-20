export type SidecarProjectConfig = {
  projectId: string;
  projectWide: boolean;
};

let cachedProjectConfig: SidecarProjectConfig | null = null;

export async function getSidecarProjectConfig(): Promise<SidecarProjectConfig> {
  if (cachedProjectConfig) {
    return cachedProjectConfig;
  }

  const response = await fetch("/api/sidecar/config", {
    method: "GET",
    cache: "no-store",
  });
  const data = (await response.json().catch(() => ({}))) as {
    project_id?: unknown;
    project_wide?: unknown;
  };
  if (
    !response.ok ||
    typeof data.project_id !== "string" ||
    !data.project_id.trim()
  ) {
    throw new Error("Failed to resolve sidecar project");
  }

  cachedProjectConfig = {
    projectId: data.project_id.trim(),
    projectWide: data.project_wide === true,
  };
  return cachedProjectConfig;
}

export async function getSidecarProjectId(): Promise<string> {
  return (await getSidecarProjectConfig()).projectId;
}
