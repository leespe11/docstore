import { NextRequest, NextResponse } from "next/server";

const BACKEND_URL = process.env.BACKEND_URL || "http://backend:8000";

export const dynamic = "force-dynamic";

export async function POST(req: NextRequest) {
  const payload = await req.text();
  const resp = await fetch(`${BACKEND_URL}/documents/download-zip`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: payload,
  });
  // Binary passthrough (a zip file), not text — everything else in this
  // app's proxy routes forwards JSON, but this one forwards the file bytes.
  const body = await resp.arrayBuffer();
  return new NextResponse(body, {
    status: resp.status,
    headers: {
      "Content-Type": resp.headers.get("Content-Type") || "application/zip",
      "Content-Disposition": resp.headers.get("Content-Disposition") || 'attachment; filename="documents.zip"',
    },
  });
}
