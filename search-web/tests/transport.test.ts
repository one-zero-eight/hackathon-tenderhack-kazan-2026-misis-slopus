import assert from "node:assert/strict";
import { test } from "node:test";
import { createServer } from "node:http";
import type { AddressInfo } from "node:net";
import { apiAutofillCharacteristics, apiCatalogStats, apiGetProduct, apiGetSimilarProducts, apiImageUrl, apiSearchProducts } from "../src/lib/api";
import { apiSearchProducts as serverSearchProducts } from "../src/lib/server/search-api";
import { GET, POST } from "../src/routes/api/search/[...path]";
import type { APIEvent } from "@solidjs/start/server";
import { suggestFromNeighbors } from "../src/lib/server/neighbor-suggest";
import { getSoleSpgzForKpgz } from "../src/lib/server/classifiers";
import { embedTexts } from "../src/lib/server/embeddings";
import { DIMENSIONS, DOCUMENT_PROMPT, MODEL_ID, QUERY_PROMPT } from "../src/lib/server/config";

test("cabinet gets the sole active SPGZ schema even when STE neighbors have no SPGZ", async () => {
  const originalFetch = globalThis.fetch;
  try {
    globalThis.fetch = async () => Response.json({ items: [{
      id: 36311645, name: "Шкаф архивный металлический", score: 0.8,
      okpd2: "31.01.11.122", kpgzCode: "01.16.07.01.01", spgzId: null,
      characteristics: [],
    }] });
    const draft = await suggestFromNeighbors("Шкаф металлический для медицинских карт, 1200х450х1800 мм (ДхШхВ), 5 полок, замок");
    assert.equal(draft.spgz?.code, "1241433623");
    assert.equal(draft.ktru?.code, "31.01.11.122-00000002");
    for (const name of ["Ширина", "Высота", "Количество полок"]) {
      assert.equal(draft.characteristics.find(field => field.name === name)?.numeric, true);
    }
    assert.ok(draft.characteristics.some(field => field.name === "Требования к фурнитуре"));
    assert.equal(getSoleSpgzForKpgz("nonexistent"), null);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("autofill transport sends the actual schema, supports abort and exposes API errors", async () => {
  const originalFetch = globalThis.fetch;
  const controller = new AbortController();
  const input = {
    text: "Молоток 500 г",
    characteristics: [{ id: "mass", name: "Масса", options: [], optionLabels: {}, ranges: [], unit: "кг", multiple: false, hint: "" }],
    standards: ["ГОСТ Р 58518-2019"],
  };
  try {
    globalThis.fetch = async (url, init) => {
      assert.equal(url, "/api/search/characteristics/autofill");
      assert.equal(init?.method, "POST");
      assert.equal(init?.signal, controller.signal);
      assert.deepEqual(JSON.parse(String(init?.body)), input);
      assert.ok(!JSON.stringify(init).includes("TYPESAFE_API_KEY"));
      return Response.json({ characteristics: [{ id: "mass", value: "0.5", rangeOption: "", confidence: 0.9, evidence: "500 г" }], optionProbabilities: {}, model: "jev", durationMs: 100, usage: {} });
    };
    const result = await apiAutofillCharacteristics(input, controller.signal);
    assert.equal(result.characteristics[0].value, "0.5");
    globalThis.fetch = async () => Response.json({ detail: "TypeSafe API: HTTP 429" }, { status: 502 });
    await assert.rejects(apiAutofillCharacteristics(input), { message: "TypeSafe API: HTTP 429" });
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("catalog browser transport uses only same-origin Solid API URLs", async () => {
  const originalFetch = globalThis.fetch;
  const urls: string[] = [];
  try {
    globalThis.fetch = async (url) => {
      urls.push(String(url));
      return Response.json({});
    };
    await apiCatalogStats();
    await apiSearchProducts({ query: "шкаф", mode: "text" });
    await apiGetProduct(123);
    await apiGetSimilarProducts(123);
    assert.deepEqual(urls, [
      "/api/search/catalog/stats", "/api/search/catalog/search",
      "/api/search/catalog/products/123", "/api/search/catalog/products/123/similar",
    ]);
    assert.equal(apiImageUrl(456), "/api/search/images/456");
  } finally {
    globalThis.fetch = originalFetch;
  }
});

function proxyEvent(path: string, init?: RequestInit): Pick<APIEvent, "request" | "params"> {
  const request = new Request(`http://192.168.1.10:3000/api/search/${path}`, init);
  return { request, params: { path } };
}

test("Solid proxy forwards requests, errors and binary images to the server-only API", async () => {
  const originalFetch = globalThis.fetch;
  const previousUrl = process.env.SEARCH_API_URL;
  process.env.SEARCH_API_URL = "http://127.0.0.1:18070/";
  try {
    const body = JSON.stringify({ query: "шкаф", mode: "text", page: 2, categoryId: 5 });
    const event = proxyEvent("catalog/search", {
      method: "POST", body,
      headers: { "Content-Type": "application/json", Cookie: "private=value", Origin: "http://192.168.1.10:3000" },
    });
    globalThis.fetch = async (url, init) => {
      assert.equal(url, "http://127.0.0.1:18070/catalog/search");
      assert.equal(init?.method, "POST");
      assert.equal(init?.body, body);
      assert.equal(init?.signal, event.request.signal);
      const headers = new Headers(init?.headers);
      assert.equal(headers.get("Content-Type"), "application/json");
      assert.equal(headers.get("Cookie"), null);
      assert.equal(headers.get("Origin"), null);
      return Response.json({ items: [] });
    };
    assert.deepEqual(await (await POST(event)).json(), { items: [] });

    globalThis.fetch = async () => Response.json({ detail: "TypeSafe API: HTTP 429" }, {
      status: 502, headers: { "Retry-After": "30" },
    });
    const error = await POST(proxyEvent("characteristics/autofill", { method: "POST", body: "{}" }));
    assert.equal(error.status, 502);
    assert.equal(error.headers.get("Retry-After"), "30");
    assert.deepEqual(await error.json(), { detail: "TypeSafe API: HTTP 429" });

    const image = new Uint8Array([0xff, 0xd8, 0xff, 0x00]);
    globalThis.fetch = async (url) => {
      assert.equal(url, "http://127.0.0.1:18070/images/456");
      return new Response(image, { headers: {
        "Content-Type": "image/jpeg", "Cache-Control": "public, max-age=604800", "X-Content-Type-Options": "nosniff",
      } });
    };
    const response = await GET(proxyEvent("images/456", { headers: { "Sec-Fetch-Dest": "image" } }));
    assert.equal(response.headers.get("Content-Type"), "image/jpeg");
    assert.equal(response.headers.get("Cache-Control"), "public, max-age=604800");
    assert.equal(response.headers.get("X-Content-Type-Options"), "nosniff");
    assert.deepEqual(new Uint8Array(await response.arrayBuffer()), image);

    globalThis.fetch = async () => { throw new Error("Connection refused"); };
    const unavailable = await GET(proxyEvent("catalog/stats"));
    assert.equal(unavailable.status, 502);
    assert.deepEqual(await unavailable.json(), { detail: "Сервер поиска недоступен" });

    globalThis.fetch = async () => { assert.fail("Unknown routes must not reach FastAPI"); };
    for (const path of ["health", "images/../health", "catalog/products/1/other", "https://example.com", "catalog/search"]) {
      assert.equal((await GET(proxyEvent(path))).status, 404);
    }
    assert.equal((await POST(proxyEvent("catalog/stats", { method: "POST" }))).status, 404);
  } finally {
    globalThis.fetch = originalFetch;
    if (previousUrl === undefined) delete process.env.SEARCH_API_URL;
    else process.env.SEARCH_API_URL = previousUrl;
  }
});

test("server-side neighbor search calls FastAPI directly without a Solid proxy round trip", async () => {
  const originalFetch = globalThis.fetch;
  const previousUrl = process.env.SEARCH_API_URL;
  process.env.SEARCH_API_URL = "http://127.0.0.1:18070";
  try {
    globalThis.fetch = async (url, init) => {
      assert.equal(url, "http://127.0.0.1:18070/catalog/search");
      assert.deepEqual(JSON.parse(String(init?.body)), { query: "шкаф", mode: "semantic", page: 1 });
      return Response.json({ items: [] });
    };
    assert.deepEqual(await serverSearchProducts({ query: "шкаф", mode: "semantic" }), { items: [] });
  } finally {
    globalThis.fetch = originalFetch;
    if (previousUrl === undefined) delete process.env.SEARCH_API_URL;
    else process.env.SEARCH_API_URL = previousUrl;
  }
});

test("meow HTTP transport uses EmbeddingGemma retrieval prompts, task routing, and base64 response", async () => {
  const requests: { dense_model_id: string; dense_task: string; dense_prompt: string; texts: string[] }[] = [];
  const server = createServer(async (req, res) => {
    assert.equal(req.url, "/embed");
    const chunks: Buffer[] = [];
    for await (const chunk of req) chunks.push(Buffer.from(chunk));
    const body = JSON.parse(Buffer.concat(chunks).toString());
    requests.push(body);
    const bytes = Buffer.alloc(DIMENSIONS * 4 * body.texts.length);
    for (let i = 0; i < body.texts.length; i++) bytes.writeFloatLE(1, i * DIMENSIONS * 4);
    res.setHeader("Content-Type", "application/json");
    res.end(JSON.stringify({ dense: { model_id: MODEL_ID, dtype: "float32", shape: [body.texts.length, DIMENSIONS], data: bytes.toString("base64") } }));
  });
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  const previous = process.env.MEOW_EMBED_URL;
  process.env.MEOW_EMBED_URL = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
  try {
    assert.equal((await embedTexts(["title: товар | text: описание", "title: ещё | text: текст"], "document")).length, 2);
    assert.equal((await embedTexts(["запрос"], "query"))[0].length, DIMENSIONS * 4);
    assert.equal(requests[0].dense_model_id, "google/embeddinggemma-2");
    assert.equal(requests[0].dense_prompt, DOCUMENT_PROMPT);
    assert.equal(requests[0].dense_task, "document");
    assert.equal(requests[1].dense_prompt, QUERY_PROMPT);
    assert.deepEqual(requests[1].texts, ["запрос"]);
  } finally {
    if (previous === undefined) delete process.env.MEOW_EMBED_URL;
    else process.env.MEOW_EMBED_URL = previous;
    await new Promise<void>((resolve, reject) => server.close((error) => error ? reject(error) : resolve()));
  }
});
