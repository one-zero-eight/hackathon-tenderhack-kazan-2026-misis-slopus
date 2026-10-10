import type { CatalogStats, CharacteristicAutofillInput, CharacteristicAutofillResult, Product, ProductHit, SearchInput, SearchResult } from "./types";

async function api<T>(path: string, init?: RequestInit): Promise<T> {
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

export const apiAutofillCharacteristics = (input: CharacteristicAutofillInput, signal?: AbortSignal) =>
  api<CharacteristicAutofillResult>("/characteristics/autofill", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
    signal,
  });

export const apiImageUrl = (id: number) => `/api/search/images/${id}`;
