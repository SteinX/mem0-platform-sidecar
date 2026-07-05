export function getSidecarProjectId(): string {
  return process.env.NEXT_PUBLIC_MEM0_SIDECAR_PROJECT_ID?.trim() || "default";
}
