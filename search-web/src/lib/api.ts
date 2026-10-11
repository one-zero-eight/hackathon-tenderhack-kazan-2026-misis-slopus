import type { NeighborSuggestion, NeighborSuggestionProgress } from "./server/neighbor-suggest";
import type { CatalogStats, CharacteristicAutofillInput, CharacteristicAutofillResult, Product, ProductHit, SearchInput, SearchResult } from "./types";

export type { NeighborSuggestion, NeighborSuggestionProgress };

async function fetchApi(path: string, init?: RequestInit): Promise<Response> {
  const response = await fetch(`/api/search${path}`, {
    ...init,
    headers: { Accept: "application/json", ...(init?.headers ?? {}) },
  });
  if (!response.ok) {
    const text = await response.text();
    let detail = text;
    if (response.headers.get("Content-Type")?.includes("application/json")) {
      const body = JSON.parse(text) as { detail?: unknown };
      if (typeof body.detail === "string") detail = body.detail;
    }
    throw new Error(detail.slice(0, 400) || `API HTTP ${response.status}`);
  }
  return response;
}

async function api<T>(path: string, init?: RequestInit): Promise<T> {
  return (await fetchApi(path, init)).json() as Promise<T>;
}

export function isAbortError(error: unknown): boolean {
  return (error instanceof DOMException && error.name === "AbortError")
    || (error instanceof Error && error.name === "AbortError");
}

export const apiCatalogStats = () => api<CatalogStats>("/catalog/stats");

export const apiSearchProducts = (input: SearchInput) =>
  api<SearchResult>("/catalog/search", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      query: input.query,
      mode: input.mode,
      page: input.page ?? 1,
      categoryId: input.categoryId,
    }),
  });

export const apiGetProduct = (id: number) => api<Product>(`/catalog/products/${id}`);

export const apiGetSimilarProducts = (id: number) => api<ProductHit[]>(`/catalog/products/${id}/similar`);

export const apiAutofillCharacteristics = (input: CharacteristicAutofillInput, signal?: AbortSignal) =>
  api<CharacteristicAutofillResult>("/characteristics/autofill", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
    signal,
  });

export const apiSuggestCategories = (
  description: string,
  options: { force?: boolean } = {},
  signal?: AbortSignal,
) =>
  api<NeighborSuggestion>("/categories/suggest", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ description, force: options.force === true }),
    signal,
  });

export async function apiSuggestCategoriesStream(
  description: string,
  options: { force?: boolean },
  signal: AbortSignal,
  onProgress: (progress: NeighborSuggestionProgress) => void,
): Promise<NeighborSuggestion> {
  const started = performance.now();
  let neighborsDurationMs: number | undefined;
  const response = await fetchApi("/categories/suggest/stream", {
    method: "POST",
    headers: { Accept: "application/x-ndjson", "Content-Type": "application/json" },
    body: JSON.stringify({ description, force: options.force === true }),
    signal,
  });
  if (!response.body) throw new Error("Сервер не вернул поток результатов.");
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  type Event =
    | { type: "progress"; data: NeighborSuggestionProgress }
    | { type: "result"; data: NeighborSuggestion }
    | { type: "error"; detail: string };
  try {
    while (true) {
      const { value, done } = await reader.read();
      buffer += decoder.decode(value, { stream: !done });
      let newline: number;
      while ((newline = buffer.indexOf("\n")) >= 0 || (done && buffer.length > 0)) {
        const line = newline >= 0 ? buffer.slice(0, newline) : buffer;
        buffer = newline >= 0 ? buffer.slice(newline + 1) : "";
        if (!line.trim()) continue;
        signal.throwIfAborted();
        const event = JSON.parse(line) as Event;
        if (event.type === "error") throw new Error(event.detail);
        if (event.type === "result") return { ...event.data, neighborsDurationMs };
        if (event.data.neighbors !== undefined) {
          neighborsDurationMs ??= Math.round(performance.now() - started);
          onProgress({ ...event.data, neighborsDurationMs });
        } else {
          onProgress(event.data);
        }
      }
      if (done) throw new Error("Поток результатов завершился до получения категорий.");
    }
  } finally {
    await reader.cancel();
    reader.releaseLock();
  }
}

export const apiImageUrl = (id: number) => `/api/search/images/${id}`;
