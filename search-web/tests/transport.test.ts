import assert from "node:assert/strict";
import { test } from "node:test";
import { apiAutofillCharacteristics, apiCatalogStats, apiGetProduct, apiGetSimilarProducts, apiImageUrl, apiSearchProducts } from "../src/lib/api";
import { apiCatalogCharacteristics, apiSearchProducts as serverSearchProducts } from "../src/lib/server/search-api";
import { GET, POST } from "../src/routes/api/search/[...path]";
import type { APIEvent } from "@solidjs/start/server";
import { suggestFromNeighbors } from "../src/lib/server/neighbor-suggest";
test("category suggestions delegate voting and schema assembly to Python once", async () => {
  const originalFetch = globalThis.fetch;
  const description = "Шкаф металлический, 5 полок";
  const expected = {
    neighbors: [],
    spgz: { code: "1241433623", name: "Шкаф", score: 1, source: "neighbors" },
    characteristics: [{ id: "width", name: "Ширина", numeric: true, source: "spgz" }],
    okpdKtruDecision: { needsChoice: false, reason: null, alternatives: [] },
    kpgzSpgzDecision: { needsChoice: false, reason: null, alternatives: [] },
  };
  let calls = 0;
  try {
    globalThis.fetch = async (url, init) => {
      calls++;
      assert.ok(String(url).endsWith("/categories/suggest"));
      assert.equal(init?.method, "POST");
      assert.deepEqual(JSON.parse(String(init?.body)), { description, force: false });
      return Response.json(expected);
    };
    assert.deepEqual(await suggestFromNeighbors(description), expected);
    assert.equal(calls, 1);
    globalThis.fetch = async () => Response.json({ detail: "Нет справочников" }, { status: 503 });
    await assert.rejects(suggestFromNeighbors(description), { message: "Нет справочников" });
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("autofill transport sends the actual schema, supports abort and exposes API errors", async () => {
  const originalFetch = globalThis.fetch;
  const controller = new AbortController();
  const input = {
    text: "Молоток 500 г",
    characteristics: [{
      id: "mass", name: "Масса", options: [], optionLabels: {}, ranges: [], unit: "кг",
      numeric: true, multiple: false, required: true, hint: "", source: "ktru" as const, examples: ["0.5", "0.6"],
    }],
    standards: ["ГОСТ Р 58518-2019"],
    ktruCode: "25.73-01",
    additionalAllowed: true,
    excludedNames: ["Цвет"],
  };
  try {
    globalThis.fetch = async (url, init) => {
      assert.equal(url, "/api/search/characteristics/autofill");
      assert.equal(init?.method, "POST");
      assert.equal(init?.signal, controller.signal);
      assert.deepEqual(JSON.parse(String(init?.body)), input);
      assert.ok(!JSON.stringify(init).includes("TYPESAFE_API_KEY"));
      return Response.json({
        status: "accepted",
        characteristics: [{
          id: "mass", value: "0.5", rangeOption: "", status: "accepted", confidence: 0.9,
          quote: "500 г", errors: [],
          evidence: { quote: "500 г", start: 8, end: 13, source: "llm", conversion: { from: "г", to: "кг", factor: 0.001, result: 0.5 } },
        }],
        additional_fields: [],
        additional_allowed: true,
        product: {
          model: { name: "Модель", value: null, quote: null, status: "missing", source: null, errors: [], evidence: null },
          manufacturer: { name: "Производитель", value: null, quote: null, status: "missing", source: null, errors: [], evidence: null },
          article: { name: "Артикул", value: null, quote: null, status: "missing", source: null, errors: [], evidence: null },
        },
        issues: [], threshold: 0.85, filled: 1, total: 1,
        model: "ggml-org/gemma-4-E4B-it-GGUF:Q8_0", durationMs: 100,
        usage: { input_tokens: 1, output_tokens: 1 },
      });
    };
    const result = await apiAutofillCharacteristics(input, controller.signal);
    assert.equal(result.characteristics[0].value, "0.5");
    assert.equal(result.status, "accepted");
    assert.equal(result.product.article.status, "missing");
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

    globalThis.fetch = async (url) => {
      assert.equal(url, "http://127.0.0.1:18070/classifiers/search");
      return Response.json({ okpdKtru: [], kpgzSpgz: [], hits: [], needsChoice: false });
    };
    const classifier = await POST(proxyEvent("classifiers/search", {
      method: "POST", body: JSON.stringify({ query: "молоток" }),
      headers: { "Content-Type": "application/json" },
    }));
    assert.equal(classifier.status, 200);
    globalThis.fetch = async (url, init) => {
      assert.equal(url, "http://127.0.0.1:18070/categories/suggest");
      assert.deepEqual(JSON.parse(String(init?.body)), { description: "молоток" });
      return Response.json({ neighbors: [], characteristics: [] });
    };
    const suggestion = await POST(proxyEvent("categories/suggest", {
      method: "POST", body: JSON.stringify({ description: "молоток" }),
      headers: { "Content-Type": "application/json" },
    }));
    assert.equal(suggestion.status, 200);
  } finally {
    globalThis.fetch = originalFetch;
    if (previousUrl === undefined) delete process.env.SEARCH_API_URL;
    else process.env.SEARCH_API_URL = previousUrl;
  }
});

test("catalog characteristic transport requests the selected category and exposes errors", async () => {
  const originalFetch = globalThis.fetch;
  const previousUrl = process.env.SEARCH_API_URL;
  process.env.SEARCH_API_URL = "http://127.0.0.1:18070";
  try {
    globalThis.fetch = async (url) => {
      assert.equal(url, "http://127.0.0.1:18070/catalog/characteristics?kpgzCode=01.16.07");
      return Response.json([{ id: "catalog:color", name: "Цвет" }]);
    };
    assert.equal((await apiCatalogCharacteristics("01.16.07"))[0].name, "Цвет");
    globalThis.fetch = async () => { assert.fail("Empty category should not query all products"); };
    assert.deepEqual(await apiCatalogCharacteristics(""), []);
    globalThis.fetch = async () => Response.json({ detail: "Нет каталога" }, { status: 503 });
    await assert.rejects(apiCatalogCharacteristics("01.16.07"), { message: "Нет каталога" });
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