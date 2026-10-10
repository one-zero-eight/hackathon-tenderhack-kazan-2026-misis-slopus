import assert from "node:assert/strict";
import { test } from "node:test";
import { createServer } from "node:http";
import type { AddressInfo } from "node:net";
import { embedTexts } from "../src/lib/server/embeddings";
import { DIMENSIONS, DOCUMENT_PROMPT, MODEL_ID, QUERY_PROMPT } from "../src/lib/server/config";

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
