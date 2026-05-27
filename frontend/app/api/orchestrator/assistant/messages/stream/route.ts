const ORCHESTRATOR_PROXY_TARGET =
  process.env.ORCHESTRATOR_PROXY_TARGET?.trim() || "http://127.0.0.1:8020";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

export async function POST(request: Request): Promise<Response> {
  const target = `${ORCHESTRATOR_PROXY_TARGET.replace(/\/$/, "")}/assistant/messages/stream`;
  const headers = new Headers();
  headers.set("content-type", request.headers.get("content-type") || "application/json");
  const requestId = request.headers.get("x-request-id");
  if (requestId) headers.set("x-request-id", requestId);

  const upstream = await fetch(target, {
    method: "POST",
    headers,
    body: request.body,
    cache: "no-store",
    duplex: "half",
  } as RequestInit & { duplex: "half" });

  const responseHeaders = new Headers();
  responseHeaders.set("Content-Type", upstream.headers.get("content-type") || "text/event-stream; charset=utf-8");
  responseHeaders.set("Cache-Control", "no-cache, no-transform");
  responseHeaders.set("Connection", "keep-alive");
  const upstreamRequestId = upstream.headers.get("x-request-id");
  if (upstreamRequestId) responseHeaders.set("x-request-id", upstreamRequestId);

  return new Response(upstream.body, {
    status: upstream.status,
    statusText: upstream.statusText,
    headers: responseHeaders,
  });
}
