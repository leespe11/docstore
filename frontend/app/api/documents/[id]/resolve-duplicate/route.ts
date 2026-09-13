import { NextRequest, NextResponse } from "next/server";

const BACKEND_URL = process.env.BACKEND_URL || "http://backend:8000";

export const dynamic = "force-dynamic";

export async function POST(req: NextRequest, { params }: { params: { id: string } }) {
  const payload = await req.text();
  const resp = await fetch(`${BACKEND_URL}/documents/${params.id}/resolve-duplicate`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: payload,
  });
  const body = await resp.text();
  return new NextResponse(body, {
    status: resp.status,
    headers: { "Content-Type": "application/json" },
  });
}
