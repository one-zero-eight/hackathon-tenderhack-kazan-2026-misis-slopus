import type { APIEvent } from "@solidjs/start/server";
import { fetchSearchApi } from "../../../lib/server/search-api";

async function proxy({ request, params }: Pick<APIEvent, "request" | "params">): Promise<Response> {
  const path = `/${params.path}`;
  const allowed = request.method === "GET"
    ? /^\/(?:catalog\/stats|catalog\/products\/[1-9]\d*(?:\/similar)?|images\/[1-9]\d*)$/.test(path)
    : request.method === "POST" && ["/catalog/search", "/characteristics/autofill", "/classifiers/search", "/categories/suggest", "/categories/suggest/stream"].includes(path);
  if (!allowed) return new Response("Not found", { status: 404 });

  const headers = new Headers({ Accept: request.headers.get("Accept") || "application/json" });
  const contentType = request.headers.get("Content-Type");
  if (contentType) headers.set("Content-Type", contentType);

  let response: Response;
  try {
    response = await fetchSearchApi(path, {
      method: request.method,
      headers,
      body: request.method === "POST" ? await request.text() : undefined,
      signal: request.signal,
      redirect: "error",
    });
  } catch (error) {
    if (request.signal.aborted) throw error;
    return Response.json({ detail: "Сервер поиска недоступен" }, { status: 502 });
  }

  const responseHeaders = new Headers();
  for (const name of ["Content-Type", "Cache-Control", "X-Content-Type-Options", "Retry-After"]) {
    const value = response.headers.get(name);
    if (value) responseHeaders.set(name, value);
  }
  return new Response(response.body, { status: response.status, headers: responseHeaders });
}

export const GET = proxy;
export const POST = proxy;
