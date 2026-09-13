import { NextRequest, NextResponse } from "next/server";

const BACKEND_URL = process.env.BACKEND_URL || "http://backend:8000";

export const dynamic = "force-dynamic";

export async function GET(req: NextRequest) {
  const url = `${BACKEND_URL}/documents${req.nextUrl.search}`;
  const resp = await fetch(url, { cache: "no-store" });
  const body = await resp.text();
  return new NextResponse(body, {
    status: resp.status,
    headers: { "Content-Type": "application/json" },
  });
}

export async function POST(req: NextRequest) {
  // Pass the multipart form straight through so we don't have to re-implement
  // multipart encoding here.
  const incoming = await req.formData();
  const forward = new FormData();
  for (const [key, value] of incoming.entries()) {
    forward.append(key, value as any);
  }

  const resp = await fetch(`${BACKEND_URL}/documents`, {
    method: "POST",
    body: forward,
  });
  const body = await resp.text();
  return new NextResponse(body, {
    status: resp.status,
    headers: { "Content-Type": "application/json" },
  });
}
