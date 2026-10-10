import type { CatalogStats, Product, ProductHit, SearchInput, SearchResult } from "./types";

export const searchApiUrl = () =>
  (import.meta as ImportMeta & { env?: Record<string, string> }).env?.VITE_SEARCH_API_URL
  || (typeof process !== "undefined" ? process.env.SEARCH_API_URL : undefined)
  || "http://127.0.0.1:8070";

async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${searchApiUrl().replace(/\/+$/, "")}${path}`, {
    ...init,
    headers: { Accept: "application/json", ...(init?.headers ?? {}) },
  });
  if (!response.ok) {
    const detail = (await response.text()).slice(0, 400);
    throw new Error(detail || `API HTTP ${response.status}`);
  }
  return response.json() as Promise<T>;
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

/** Image URLs must hit FastAPI — Nitro 404s `<img>` to `/api/images/*` (Sec-Fetch-Dest: image). */
export const apiImageUrl = (id: number) => `${searchApiUrl().replace(/\/+$/, "")}/images/${id}`;
