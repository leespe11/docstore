import { NextResponse } from "next/server";

const BACKEND_URL = process.env.BACKEND_URL || "http://backend:8000";

export const dynamic = "force-dynamic";

export async function GET() {
  const resp = await fetch(`${BACKEND_URL}/documents/categories`, { cache: "no-store" });
  const body = await resp.text();
  return new NextResponse(body, {
    status: resp.status,
    headers: { "Content-Type": "application/json" },
  });
}
