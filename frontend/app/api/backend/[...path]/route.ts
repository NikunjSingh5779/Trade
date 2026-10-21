import { NextRequest, NextResponse } from "next/server";

const BACKEND_BASE = (
  process.env.BACKEND_API_BASE_URL || "http://127.0.0.1:8000"
).replace(/\/+$/, "");

async function proxyToLocalBackend(
  request: NextRequest,
  context: { params: Promise<{ path: string[] }> }
): Promise<NextResponse> {
  const token = process.env.LOCAL_API_TOKEN?.trim();
  if (!token) {
    return NextResponse.json(
      { detail: "Local API token is unavailable. Restart Trade with scripts/dev.ps1." },
      { status: 503 }
    );
  }

  const { path } = await context.params;
  if (
    !Array.isArray(path) ||
    path.length === 0 ||
    path[0] !== "api" ||
    path.some((segment) => !/^[A-Za-z0-9._-]+$/.test(segment) || segment === "..")
  ) {
    return NextResponse.json({ detail: "Invalid backend route." }, { status: 400 });
  }

  const target = new URL(`/${path.map(encodeURIComponent).join("/")}`, `${BACKEND_BASE}/`);
  target.search = request.nextUrl.search;

  const headers = new Headers({
    Authorization: `Bearer ${token}`,
    Accept: "application/json"
  });
  const contentType = request.headers.get("content-type");
  if (contentType) headers.set("Content-Type", contentType);

  try {
    const body = request.method === "GET" ? undefined : await request.arrayBuffer();
    const upstream = await fetch(target, {
      method: request.method,
      headers,
      body,
      cache: "no-store",
      redirect: "manual"
    });
    const responseHeaders = new Headers();
    for (const name of ["content-type", "cache-control", "retry-after"]) {
      const value = upstream.headers.get(name);
      if (value) responseHeaders.set(name, value);
    }
    return new NextResponse(upstream.body, {
      status: upstream.status,
      headers: responseHeaders
    });
  } catch {
    return NextResponse.json(
      { detail: "Local Trade backend is unavailable. Check that port 8000 is running." },
      { status: 502 }
    );
  }
}

export const GET = proxyToLocalBackend;
export const POST = proxyToLocalBackend;
