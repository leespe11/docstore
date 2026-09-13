import { NextResponse } from "next/server";

const BACKEND_URL = process.env.BACKEND_URL || "http://backend:8000";

export const dynamic = "force-dynamic";

export async function GET() {
  try {
    const resp = await fetch(`${BACKEND_URL}/speech/status`, { cache: "no-store" });
    const body = await resp.text();
    return new NextResponse(body, {
      status: resp.status,
      headers: { "Content-Type": "application/json" },
    });
  } catch {
    // Backend unreachable, not just the speech service — same end result
    // for the mic button either way.
    return NextResponse.json({ available: false });
  }
}
