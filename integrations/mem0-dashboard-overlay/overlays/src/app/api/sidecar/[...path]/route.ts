import { NextResponse } from "next/server";

export async function GET() {
  return NextResponse.json({ message: "sidecar overlay placeholder" }, { status: 501 });
}
