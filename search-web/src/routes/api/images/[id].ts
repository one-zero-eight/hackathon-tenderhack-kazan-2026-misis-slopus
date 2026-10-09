import { mkdir, readFile, rename, writeFile } from "node:fs/promises";
import { dirname, join } from "node:path";
import type { APIEvent } from "@solidjs/start/server";
import { getDatabase } from "../../../lib/server/database";
import { searchDatabasePath } from "../../../lib/server/config";

const inFlight = new Map<number, Promise<Uint8Array>>();
const unavailable = new Map<number, number>();

export async function GET({ params }: APIEvent): Promise<Response> {
  const id = Number(params.id);
  if (!/^\d+$/.test(params.id) || !Number.isSafeInteger(id) || id <= 0) return new Response("Not found", { status: 404 });
  const cachedPath = join(dirname(searchDatabasePath()), "images", `${id}.image`);
  try {
    const bytes = await readFile(cachedPath);
    return imageResponse(bytes);
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
  }
  if ((unavailable.get(id) ?? 0) > Date.now()) return new Response("Image unavailable", { status: 404 });
  // Accept only image identifiers actually present in the local catalog, not arbitrary URLs.
  const known = getDatabase().sqlite.prepare("SELECT 1 FROM products,json_each(products.image_ids) image WHERE image.value=? LIMIT 1").get(id);
  if (!known) return new Response("Not found", { status: 404 });
  let request = inFlight.get(id);
  if (!request) {
    request = downloadImage(id, cachedPath).finally(() => inFlight.delete(id));
    inFlight.set(id, request);
  }
  try {
    return imageResponse(await request);
  } catch {
    unavailable.set(id, Date.now() + 300000);
    if (unavailable.size > 2000) unavailable.delete(unavailable.keys().next().value!);
    return new Response("Image unavailable", { status: 502, headers: { "Cache-Control": "no-store" } });
  }
}

function imageMime(bytes: Uint8Array): string {
  if (bytes[0] === 0xff && bytes[1] === 0xd8 && bytes[2] === 0xff) return "image/jpeg";
  if (bytes[0] === 0x89 && bytes[1] === 0x50 && bytes[2] === 0x4e && bytes[3] === 0x47) return "image/png";
  if (String.fromCharCode(...bytes.slice(0, 3)) === "GIF") return "image/gif";
  if (String.fromCharCode(...bytes.slice(0, 4)) === "RIFF" && String.fromCharCode(...bytes.slice(8, 12)) === "WEBP") return "image/webp";
  throw new Error("Unsupported image format");
}

function imageResponse(bytes: Uint8Array): Response {
  return new Response(bytes as Uint8Array<ArrayBuffer>, {
    headers: { "Content-Type": imageMime(bytes), "Cache-Control": "public, max-age=604800", "X-Content-Type-Options": "nosniff" },
  });
}

async function downloadImage(id: number, path: string): Promise<Uint8Array> {
  const response = await fetch(`https://zakupki.mos.ru/newapi/api/FileStorage/Download?id=${id}`, { signal: AbortSignal.timeout(20000), redirect: "error" });
  if (!response.ok || !response.headers.get("content-type")?.startsWith("image/")) throw new Error("Image unavailable");
  const maxBytes = 12 * 1024 * 1024;
  if (Number(response.headers.get("content-length")) > maxBytes) throw new Error("Image too large");
  const reader = response.body!.getReader();
  const chunks: Uint8Array[] = [];
  let size = 0;
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    size += value.length;
    if (size > maxBytes) { await reader.cancel(); throw new Error("Image too large"); }
    chunks.push(value);
  }
  const bytes = Buffer.concat(chunks, size);
  imageMime(bytes);
  await mkdir(dirname(path), { recursive: true });
  const temporary = `${path}.${crypto.randomUUID()}.tmp`;
  await writeFile(temporary, bytes);
  await rename(temporary, path);
  return bytes;
}
