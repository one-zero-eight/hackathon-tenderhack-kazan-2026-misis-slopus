import type {
  CatalogCharacteristic,
  ClassifierPairHit,
  ClassifierSearchResult,
  SearchInput,
  SearchResult,
} from "../types";

export const searchApiUrl = () => process.env.SEARCH_API_URL || "http://127.0.0.1:8070";

export function fetchSearchApi(path: string, init?: RequestInit): Promise<Response> {
  return fetch(`${searchApiUrl().replace(/\/+$/, "")}${path}`, init);
}

export async function apiSearchProducts(input: SearchInput): Promise<SearchResult> {
  const response = await fetchSearchApi("/catalog/search", {
    method: "POST",
    headers: { Accept: "application/json", "Content-Type": "application/json" },
    body: JSON.stringify({
      query: input.query,
      mode: input.mode,
      page: input.page ?? 1,
      categoryId: input.categoryId,
    }),
  });
  return readResponse<SearchResult>(response);
}

export async function apiSearchClassifiers(input: {
  query: string;
  mode?: "hybrid" | "semantic" | "text";
  limit?: number;
}): Promise<ClassifierSearchResult> {
  const response = await fetchSearchApi("/classifiers/search", {
    method: "POST",
    headers: { Accept: "application/json", "Content-Type": "application/json" },
    body: JSON.stringify({
      query: input.query,
      mode: input.mode ?? "hybrid",
      limit: input.limit ?? 12,
    }),
  });
  return readResponse<ClassifierSearchResult>(response);
}

export async function apiSuggestCategories(
  description: string,
  options: { force?: boolean } = {},
): Promise<import("./neighbor-suggest").NeighborSuggestion> {
  const response = await fetchSearchApi("/categories/suggest", {
    method: "POST",
    headers: { Accept: "application/json", "Content-Type": "application/json" },
    body: JSON.stringify({ description, force: options.force === true }),
  });
  return readResponse<import("./neighbor-suggest").NeighborSuggestion>(response);
}

export async function apiCatalogCharacteristics(kpgzCode: string): Promise<CatalogCharacteristic[]> {
  if (!kpgzCode) return [];
  const response = await fetchSearchApi(`/catalog/characteristics?kpgzCode=${encodeURIComponent(kpgzCode)}`);
  return readResponse<CatalogCharacteristic[]>(response);
}

export type { ClassifierPairHit };

async function readResponse<T>(response: Response): Promise<T> {
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
