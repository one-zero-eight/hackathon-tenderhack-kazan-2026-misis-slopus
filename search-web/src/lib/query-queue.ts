export type QueryStatus = "pending" | "processing" | "done" | "error" | "cancelled";

export type QueuedQuery<T> = {
  id: string;
  text: string;
  status: QueryStatus;
  result?: T;
  progress?: Partial<T>;
  durationMs?: number;
  error?: string;
};

export const normalizeQuery = (text: string) => text.trim().replace(/\s+/g, " ");

function isAbortError(error: unknown): boolean {
  return (error instanceof DOMException && error.name === "AbortError")
    || (error instanceof Error && error.name === "AbortError");
}

/** One request per physical line. Commas/semicolons inside descriptions are not columns. */
export function parseQueryFile(text: string): string[] {
  return text.replace(/^\uFEFF/, "").split(/\r\n|\n|\r/).map((line) => {
    const value = line.trim();
    return value.startsWith('"') && value.endsWith('"')
      ? value.slice(1, -1).replace(/""/g, '"').trim()
      : value;
  }).filter(Boolean);
}

export class QueryQueue<T> {
  private entries: QueuedQuery<T>[] = [];
  private requests = new Map<string, Promise<T>>();
  private controllers = new Map<string, AbortController>();
  private nextId = 0;
  private active = 0;
  private stopped = false;

  constructor(
    private process: (text: string, signal: AbortSignal, reportProgress: (progress: Partial<T>) => void) => Promise<T>,
    private changed: (items: QueuedQuery<T>[]) => void,
    private started: (text: string) => void = () => {},
  ) {}

  get items(): QueuedQuery<T>[] { return this.entries; }

  find(text: string): QueuedQuery<T> | undefined {
    const query = normalizeQuery(text);
    return this.entries.find((item) => normalizeQuery(item.text) === query);
  }

  add(texts: string[]): QueuedQuery<T>[] {
    const added = texts.map((text): QueuedQuery<T> => ({
      id: `query-${++this.nextId}`, text: text.trim(), status: "pending",
    }));
    this.entries = [...this.entries, ...added];
    this.changed(this.entries);
    return added;
  }

  /** Drop cached result so the next run() reprocesses (e.g. force after quality gate). */
  invalidate(text: string): void {
    const query = normalizeQuery(text);
    let changed = false;
    this.entries = this.entries.map((item) => {
      if (normalizeQuery(item.text) !== query || item.status === "pending") return item;
      changed = true;
      this.controllers.get(item.id)?.abort();
      return { id: item.id, text: item.text, status: "pending" as const };
    });
    if (changed) this.changed(this.entries);
  }

  /** Abort every in-flight request (e.g. suggest effect cleanup). */
  abortInFlight(): void {
    for (const controller of this.controllers.values()) controller.abort();
  }

  /** The normal queue is serial; an explicitly selected query starts immediately and cancels others. */
  run(id: string): Promise<T> {
    const item = this.entries.find((entry) => entry.id === id);
    if (!item) return Promise.reject(new Error("Запрос не найден"));
    if (item.status === "done") return Promise.resolve(item.result!);
    const running = this.requests.get(id);
    if (running) return running;
    if (this.stopped) return Promise.reject(new Error("Очередь остановлена"));

    for (const [otherId, controller] of this.controllers) {
      if (otherId !== id) controller.abort();
    }

    const controller = new AbortController();
    this.controllers.set(id, controller);
    this.active++;
    const startedAt = performance.now();
    // Register the promise before notifying observers (which can select the same request).
    const request = Promise.resolve().then(() => {
      this.started(item.text);
      if (controller.signal.aborted) throw new DOMException("Aborted", "AbortError");
      const work = this.process(item.text, controller.signal, (progress) => {
        if (controller.signal.aborted) return;
        const current = this.entries.find((entry) => entry.id === id);
        this.update(id, { progress: { ...current?.progress, ...progress } });
      });
      // Always settle on abort even if process ignores the signal.
      const aborted = new Promise<never>((_, reject) => {
        const onAbort = () => reject(new DOMException("Aborted", "AbortError"));
        if (controller.signal.aborted) onAbort();
        else controller.signal.addEventListener("abort", onAbort, { once: true });
      });
      return Promise.race([work, aborted]);
    }).then((result) => {
      this.update(id, { status: "done", result, durationMs: Math.round(performance.now() - startedAt) });
      return result;
    }, (error: unknown) => {
      if (controller.signal.aborted || isAbortError(error)) {
        this.update(id, { status: "cancelled", error: undefined, durationMs: undefined });
        throw error instanceof Error ? error : new DOMException("Aborted", "AbortError");
      }
      this.update(id, { status: "error", error: error instanceof Error ? error.message : "Не удалось обработать запрос" });
      throw error;
    }).finally(() => {
      this.requests.delete(id);
      this.controllers.delete(id);
      this.active--;
      if (!controller.signal.aborted) this.resume();
    });
    this.requests.set(id, request);
    this.update(id, { status: "processing", error: undefined, progress: undefined });
    return request;
  }

  resume(): void {
    if (this.stopped || this.active) return;
    const next = this.entries.find((item) => item.status === "pending");
    if (next) void this.run(next.id).catch(() => {}); // Error is retained on the entry; continue the queue.
  }

  stop(): void {
    this.stopped = true;
    this.abortInFlight();
  }

  private update(id: string, patch: Partial<QueuedQuery<T>>): void {
    this.entries = this.entries.map((item) => item.id === id ? { ...item, ...patch } : item);
    this.changed(this.entries);
  }
}

export type QueryHistoryItem = { text: string; requestedAt: number };
export const QUERY_HISTORY_KEY = "zakupki.query-history.v1";
export const QUERY_HISTORY_LIMIT = 200;

export function parseQueryHistory(value: string | null): QueryHistoryItem[] {
  if (!value) return [];
  const parsed: unknown = JSON.parse(value);
  if (!Array.isArray(parsed)) throw new Error("Некорректная история запросов");
  return parsed.filter((item): item is QueryHistoryItem =>
    typeof item === "object" && item !== null && typeof item.text === "string"
    && !!item.text.trim() && typeof item.requestedAt === "number" && Number.isFinite(item.requestedAt),
  ).slice(0, QUERY_HISTORY_LIMIT);
}

export function rememberQuery(history: QueryHistoryItem[], text: string, requestedAt = Date.now()): QueryHistoryItem[] {
  const query = normalizeQuery(text);
  return [{ text: text.trim(), requestedAt }, ...history.filter((item) => normalizeQuery(item.text) !== query)]
    .slice(0, QUERY_HISTORY_LIMIT);
}
