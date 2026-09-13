import { NextRequest, NextResponse } from "next/server";

const BACKEND_URL = process.env.BACKEND_URL || "http://backend:8000";

export const dynamic = "force-dynamic";

export async function GET(_req: NextRequest, { params }: { params: { id: string } }) {
  const resp = await fetch(`${BACKEND_URL}/documents/${params.id}/download`, {
    cache: "no-store",
  });
  if (!resp.ok || !resp.body) {
    return NextResponse.json({ error: "not found" }, { status: resp.status || 404 });
  }
  const headers = new Headers();
  const passthrough = ["content-type", "content-disposition", "content-length"];
  for (const h of passthrough) {
    const v = resp.headers.get(h);
    if (v) headers.set(h, v);
  }
  return new NextResponse(resp.body, { status: 200, headers });
}
