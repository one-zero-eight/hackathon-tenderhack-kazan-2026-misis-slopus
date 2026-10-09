import assert from "node:assert/strict";
import { test } from "node:test";
import { getDatabase } from "../src/lib/server/database";
import { embedQuery } from "../src/lib/server/embeddings";
import { runSearch } from "../src/lib/server/search";
import { DIMENSIONS } from "../src/lib/server/config";

test("live BERTA /embed query and sqlite-vec semantic/hybrid retrieval", { skip: process.env.LIVE_EMBED !== "1" }, async () => {
  const vector = await embedQuery("пылесборник для пылесоса");
  assert.equal(vector.length, DIMENSIONS * 4);
  const semantic = await runSearch({ query: "пылесборник для пылесоса", mode: "semantic" });
  assert.ok(semantic.items.length > 0);
  assert.ok(semantic.items.every((item) => item.score !== null));
  assert.ok(semantic.items.slice(0, 5).some((item) => /пылес/i.test(item.name)));
  const hybrid = await runSearch({ query: "Neolux", mode: "hybrid" });
  assert.ok(hybrid.items.some((item) => item.match === "both"));
  const first = semantic.items[0];
  const filtered = await runSearch({ query: "пылесборник", mode: "semantic", categoryId: first.categoryId });
  assert.ok(filtered.items.every((item) => item.categoryId === first.categoryId));
  getDatabase().sqlite.close();
});
