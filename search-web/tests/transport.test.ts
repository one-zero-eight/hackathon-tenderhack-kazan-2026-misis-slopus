import assert from "node:assert/strict";
import { test } from "node:test";
import { createServer } from "node:http";
import type { AddressInfo } from "node:net";
import { apiAutofillCharacteristics } from "../src/lib/api";
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
      assert.ok(String(url).endsWith("/characteristics/autofill"));
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
