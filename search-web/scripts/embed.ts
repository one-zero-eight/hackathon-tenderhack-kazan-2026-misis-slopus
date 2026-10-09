import { parseArgs } from "node:util";
import { openDatabase } from "../src/lib/server/database";
import { embedTexts } from "../src/lib/server/embeddings";
import { MODEL_ID } from "../src/lib/server/config";

const { values } = parseArgs({ options: { limit: { type: "string" }, batch: { type: "string" } } });
const limit = values.limit === undefined ? Infinity : Number(values.limit);
const batchSize = Number(values.batch ?? process.env.EMBED_BATCH_SIZE ?? 64);
if ((limit !== Infinity && (!Number.isSafeInteger(limit) || limit < 1)) || !Number.isSafeInteger(batchSize) || batchSize < 1 || batchSize > 128) {
  throw new Error("--limit должен быть положительным целым, --batch: 1–128");
}
const { sqlite } = openDatabase();
const owner = `${process.pid}:${crypto.randomUUID()}`;
let stopped = false;
process.on("SIGINT", () => { stopped = true; console.log("Остановка после текущего батча…"); });
process.on("SIGTERM", () => { stopped = true; });
interface Pending { id: number; document: string; hash: string; categoryId: number }
const pending = sqlite.prepare("SELECT id,document,content_hash AS hash,category_id AS categoryId FROM products WHERE embedded_hash IS NULL OR embedded_hash <> content_hash ORDER BY id LIMIT ?");
const insert = sqlite.prepare("INSERT INTO product_vectors(product_id,embedding,category_id) VALUES(?,?,?)");
const remove = sqlite.prepare("DELETE FROM product_vectors WHERE product_id=?");
const current = sqlite.prepare("SELECT content_hash AS hash FROM products WHERE id=?");
const mark = sqlite.prepare("UPDATE products SET embedded_hash=? WHERE id=? AND content_hash=?");
// A renewable SQLite lease prevents two indexers from racing on vec0. A crashed
// process releases its lease by expiry; batches have a shorter request timeout.
const lease = () => sqlite.transaction(() => {
  const row = sqlite.prepare("SELECT value FROM metadata WHERE key='indexer_lease'").get() as { value: string } | undefined;
  if (row) {
    const lock = JSON.parse(row.value) as { owner: string; expires: number };
    if (lock.owner !== owner && lock.expires > Date.now()) throw new Error("Индексатор уже запущен в другом процессе");
  }
  sqlite.prepare("INSERT INTO metadata(key,value) VALUES('indexer_lease',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value").run(JSON.stringify({ owner, expires: Date.now() + 600000 }));
}).immediate();

try {
  lease();
  const total = (sqlite.prepare("SELECT count(*) AS n FROM products WHERE embedded_hash IS NULL OR embedded_hash <> content_hash").get() as { n: number }).n;
  console.log(`${MODEL_ID}: ${total.toLocaleString("ru-RU")} карточек ожидают индексации`);
  let completed = 0;
  const started = performance.now();
  let embedMs = 0;
  let databaseMs = 0;
  while (!stopped && completed < limit) {
    lease();
    const batch = pending.all(Math.min(batchSize, limit - completed)) as Pending[];
    if (!batch.length) break;
    const embedStarted = performance.now();
    const vectors = await embedTexts(batch.map((row) => row.document), "document");
    embedMs += performance.now() - embedStarted;
    const databaseStarted = performance.now();
    let written = 0;
    sqlite.transaction(() => {
      for (let i = 0; i < batch.length; i++) {
        const row = batch[i];
        // An import can change the product while the HTTP request is running.
        if ((current.get(row.id) as { hash: string } | undefined)?.hash !== row.hash) continue;
        remove.run(BigInt(row.id));
        insert.run(BigInt(row.id), vectors[i], BigInt(row.categoryId));
        mark.run(row.hash, row.id, row.hash);
        written++;
      }
    }).immediate();
    databaseMs += performance.now() - databaseStarted;
    completed += written;
    const seconds = (performance.now() - started) / 1000;
    console.log(`Индексировано ${completed}/${Math.min(total, limit)} · ${(completed / seconds).toFixed(2)} карточек/с · embedding ${(embedMs / 1000).toFixed(1)}с · SQLite ${(databaseMs / 1000).toFixed(1)}с`);
  }
  console.log("Индекс сохранён. Повторный запуск продолжит с оставшихся карточек.");
} finally {
  sqlite.prepare("DELETE FROM metadata WHERE key='indexer_lease' AND json_extract(value,'$.owner')=?").run(owner);
  sqlite.close();
}
