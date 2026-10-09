export type SearchMode = "hybrid" | "semantic" | "text";

export interface Characteristic {
  name: string;
  value: string;
  unit: string;
}

export interface Product {
  id: number;
  name: string;
  description: string;
  category: string;
  categoryId: number;
  manufacturer: string;
  country: string;
  unit: string;
  partNumber: string;
  modelName: string;
  okpd2: string;
  okpd2Name: string;
  kpgzCode: string;
  kpgzName: string;
  spgzId: number | null;
  price: number | null;
  imageIds: number[];
  characteristics: Characteristic[];
  sourceUpdatedAt: string;
}

export interface ProductHit extends Product {
  score: number | null;
  match: "semantic" | "text" | "both" | "catalog";
}

export interface SearchInput {
  query: string;
  mode: SearchMode;
  categoryId?: number;
  page?: number;
}

export interface SearchResult {
  items: ProductHit[];
  total: number;
  hasMore: boolean;
  page: number;
  durationMs: number;
  mode: SearchMode;
}

export interface CatalogStats {
  products: number;
  embedded: number;
  model: string;
  dimensions: number;
  categories: { id: number; name: string; count: number }[];
  lastImportedAt: string | null;
}
