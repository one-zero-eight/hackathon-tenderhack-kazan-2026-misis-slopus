import assert from "node:assert/strict";
import { test } from "node:test";
import { parseQueryFile, parseQueryHistory, QueryQueue, rememberQuery, QUERY_HISTORY_LIMIT } from "../src/lib/query-queue";

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: Error) => void;
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

const flush = () => new Promise<void>((resolve) => setImmediate(resolve));

test("import preserves commas, quoted CSV rows, arbitrary lengths and ignores empty rows", () => {
  const long = "я".repeat(5000);
  assert.deepEqual(parseQueryFile(`\uFEFF  Молоток, 500 г; деревянная ручка\r\n\r\n"Шкаф ""Архив"", стальной"\rКороткий\n${long}\n`), [
    "Молоток, 500 г; деревянная ручка", 'Шкаф "Архив", стальной', "Короткий", long,
  ]);
  assert.deepEqual(parseQueryFile(" \n\r\n"), []);
});

test("queue is serial, selected query cancels the previous in-flight request", async () => {
  const jobs = [deferred<string>(), deferred<string>(), deferred<string>()];
  const started: string[] = [];
  const signals: AbortSignal[] = [];
  const history: string[] = [];
  const queue = new QueryQueue((text, signal) => {
    started.push(text);
    signals.push(signal);
    return jobs[Number(text) - 1].promise;
  }, () => {}, (text) => history.push(text));
  const items = queue.add(["1", "2", "3"]);
  queue.resume();
  await flush();
  assert.deepEqual(started, ["1"]);
  assert.deepEqual(queue.items.map((item) => item.status), ["processing", "pending", "pending"]);

  const selected = queue.run(items[2].id);
  assert.equal(queue.run(items[2].id), selected);
  await flush();
  assert.ok(signals[0].aborted);
  assert.deepEqual(started, ["1", "3"]);
  assert.equal(queue.items[0].status, "cancelled");
  jobs[2].resolve("third result");
  assert.equal(await selected, "third result");
  await flush();
  // Cancelled item is not auto-resumed; next pending ("2") runs.
  assert.deepEqual(started, ["1", "3", "2"]);
  jobs[1].resolve("second result");
  await flush();
  assert.deepEqual(queue.items.map((item) => item.status), ["cancelled", "done", "done"]);
  assert.equal(await queue.run(items[2].id), "third result");
  assert.deepEqual(history, ["1", "3", "2"]);
});

test("failed request does not block the queue and can be explicitly retried", async () => {
  let attempts = 0;
  const queue = new QueryQueue(async (text) => {
    if (text === "first" && attempts++ === 0) throw new Error("API unavailable");
    return text;
  }, () => {});
  const [first] = queue.add(["first", "second"]);
  queue.resume();
  await flush();
  assert.equal(queue.items[0].status, "error");
  assert.equal(queue.items[0].error, "API unavailable");
  assert.equal(queue.items[1].status, "done");
  assert.equal(await queue.run(first.id), "first");
  assert.equal(queue.items[0].status, "done");
  assert.equal(queue.items[0].error, undefined);
});

test("stopping queue aborts in-flight work and prevents unsent work", async () => {
  const job = deferred<string>();
  const started: string[] = [];
  let seenSignal: AbortSignal | undefined;
  const queue = new QueryQueue((text, signal) => {
    started.push(text);
    seenSignal = signal;
    return job.promise;
  }, () => {});
  const [first] = queue.add(["first", "second"]);
  const request = queue.run(first.id);
  await flush();
  queue.stop();
  assert.ok(seenSignal?.aborted);
  await assert.rejects(request, (error: unknown) => error instanceof Error && error.name === "AbortError");
  assert.deepEqual(started, ["first"]);
  assert.equal(queue.items[0].status, "cancelled");
  assert.equal(queue.items[1].status, "pending");
});

test("abortInFlight cancels the active request without resuming the queue", async () => {
  const job = deferred<string>();
  const queue = new QueryQueue((_text, signal) => {
    signal.throwIfAborted();
    return job.promise;
  }, () => {});
  const [first, second] = queue.add(["first", "second"]);
  const request = queue.run(first.id);
  await flush();
  queue.abortInFlight();
  await assert.rejects(request, (error: unknown) => error instanceof Error && error.name === "AbortError");
  assert.equal(queue.items[0].status, "cancelled");
  assert.equal(queue.items[1].status, "pending");
  // No auto-resume after abort-only cleanup.
  await flush();
  assert.equal(queue.items[1].status, "pending");
  void second;
});

test("progress arrives before completion, merges stages and ignores aborted requests", async () => {
  type Result = { quality: string; neighbors: number[] };
  const job = deferred<Result>();
  let report!: (progress: Partial<Result>) => void;
  const queue = new QueryQueue<Result>((_text, _signal, reportProgress) => {
    report = reportProgress;
    return job.promise;
  }, () => {});
  const [item] = queue.add(["молоток"]);
  const request = queue.run(item.id);
  await flush();
  report({ quality: "ok" });
  report({ neighbors: [1, 2] });
  assert.equal(queue.items[0].status, "processing");
  assert.equal(queue.items[0].result, undefined);
  assert.deepEqual(queue.items[0].progress, { quality: "ok", neighbors: [1, 2] });
  queue.abortInFlight();
  await assert.rejects(request, { name: "AbortError" });
  report({ neighbors: [3] });
  assert.deepEqual(queue.items[0].progress?.neighbors, [1, 2]);
});

test("history is normalized, deduplicated, newest first, limited and round-trips through storage", () => {
  const history = rememberQuery(rememberQuery([], "  Молоток   500 г ", 1), "Шкаф", 2);
  const updated = rememberQuery(history, "Молоток 500 г", 3);
  assert.deepEqual(updated, [{ text: "Молоток 500 г", requestedAt: 3 }, { text: "Шкаф", requestedAt: 2 }]);
  assert.deepEqual(parseQueryHistory(JSON.stringify(updated)), updated);
  assert.deepEqual(parseQueryHistory(null), []);
  assert.deepEqual(parseQueryHistory('[null,{"text":1},{"text":" ","requestedAt":1}]'), []);
  assert.throws(() => parseQueryHistory("invalid"));
  assert.throws(() => parseQueryHistory("{}"));
  const full = Array.from({ length: QUERY_HISTORY_LIMIT }, (_, i) => ({ text: `Запрос ${i}`, requestedAt: i }));
  assert.equal(rememberQuery(full, "Новый запрос").length, QUERY_HISTORY_LIMIT);
});
