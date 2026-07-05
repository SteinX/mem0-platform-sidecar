function configuredProjectId(): string {
  return (
    process.env.SIDECAR_PROJECT_ID?.trim() ||
    process.env.MEM0_SIDECAR_DEFAULT_PROJECT_ID?.trim() ||
    "default"
  );
}

export async function GET() {
  return Response.json({ project_id: configuredProjectId() });
}
