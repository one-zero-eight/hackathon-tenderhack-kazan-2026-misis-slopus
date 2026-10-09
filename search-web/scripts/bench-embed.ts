import { parseArgs } from "node:util";
import { readFileSync, writeFileSync } from "node:fs";
import { openDatabase } from "../src/lib/server/database";
import { embedTexts } from "../src/lib/server/embeddings";

const { values } = parseArgs({ options: {
  batches: { type: "string", default: "8,16,32,64" },
  reference: { type: "string" }, compare: { type: "string" },
} });
const sizes = values.batches!.split(",").map(Number);
if (sizes.some((size) => !Number.isSafeInteger(size) || size < 1 || size > 128)) throw new Error("Invalid batch sizes");
const { sqlite } = openDatabase();
const rows = sqlite.prepare("SELECT document FROM products WHERE embedded_hash IS NULL ORDER BY id LIMIT 64").all() as { document: string }[];
sqlite.close();
if (!rows.length) throw new Error("No pending products for benchmark");
const texts = rows.map((row) => row.document);
console.log(`Fixed sample: ${texts.length} documents, mean ${Math.round(texts.reduce((n, text) => n + text.length, 0) / texts.length)} characters`);
const vectors = await embedTexts(texts.slice(0, 8), "document");
if (values.reference) writeFileSync(values.reference, Buffer.concat(vectors));
if (values.compare) {
  const reference = readFileSync(values.compare);
  const actual = Buffer.concat(vectors);
  if (reference.length !== actual.length) throw new Error("Reference size mismatch");
  const cosines: number[] = [];
  for (let row = 0; row < vectors.length; row++) {
    let dot = 0, a2 = 0, b2 = 0;
    for (let col = 0; col < vectors[row].length; col += 4) {
      const offset = row * vectors[row].length + col;
      const a = reference.readFloatLE(offset), b = actual.readFloatLE(offset);
      dot += a * b; a2 += a * a; b2 += b * b;
    }
    cosines.push(dot / Math.sqrt(a2 * b2));
  }
  console.log(`Precision agreement: min cosine=${Math.min(...cosines).toFixed(7)}`);
}
for (const batch of sizes) {
  const started = performance.now();
  for (let i = 0; i < texts.length; i += batch) await embedTexts(texts.slice(i, i + batch), "document");
  const seconds = (performance.now() - started) / 1000;
  console.log(`batch=${batch}: ${seconds.toFixed(2)}s, ${(texts.length / seconds).toFixed(2)} docs/s`);
}
