import { NextRequest, NextResponse } from "next/server";

const BACKEND_URL = process.env.BACKEND_URL || "http://backend:8000";

export const dynamic = "force-dynamic";

export async function POST(req: NextRequest) {
  // Pass the multipart form straight through, same pattern as the document
  // upload proxy — a single short audio clip is nowhere near the size where
  // that proxy's large-batch buffering concern would apply.
  const incoming = await req.formData();
  const forward = new FormData();
  for (const [key, value] of incoming.entries()) {
    forward.append(key, value as any);
  }

  const resp = await fetch(`${BACKEND_URL}/speech/transcribe`, {
    method: "POST",
    body: forward,
  });
  const body = await resp.text();
  return new NextResponse(body, {
    status: resp.status,
    headers: { "Content-Type": "application/json" },
  });
}
