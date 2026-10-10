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
  ktruCode: string;
  ktruName: string;
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

export interface CategoryTreeNode {
  id: number;
  name: string;
  parentId: number | null;
  count: number;
  hasChildren: boolean;
  reconstructed: boolean;
  nameResolved: boolean;
}

export interface CatalogStats {
  products: number;
  embedded: number;
  model: string;
  dimensions: number;
  categories: { id: number; name: string; count: number }[];
  categoryTree: CategoryTreeNode[];
  lastImportedAt: string | null;
}

export type DirectoryTab = "ktru" | "kpgz" | "spgz" | "okpd" | "okei";

export type VotedCode = { code: string; name: string; score: number };

export type CharacteristicRange = {
  option: string;
  min: number | null;
  max: number | null;
  minInclusive: boolean;
  maxInclusive: boolean;
};

export type DraftCharacteristic = {
  id: string;
  name: string;
  value: string;
  /** Selected KTRU range bucket for quantitative range characteristics. */
  rangeOption: string;
  required: boolean;
  badges: string[];
  hint: string;
  options: string[];
  optionLabels: Record<string, string>;
  ranges: CharacteristicRange[];
  unit: string;
  multiple: boolean;
  source: "ktru" | "spgz" | "both" | "custom";
  custom?: boolean;
};

export interface DirTreeNode {
  id: string;
  code: string;
  name: string;
  kind: string;
  childCount: number;
  hasChildren: boolean;
  meta?: Record<string, string | number | boolean | null>;
}

export interface DirChildrenPage {
  parentId: string | null;
  nodes: DirTreeNode[];
  total: number;
  offset: number;
  hasMore: boolean;
  filterMatches: number | null;
}

export type KtruNodeKind = "okpd" | "ktru";

export interface KtruTreeNode {
  id: string;
  code: string;
  name: string;
  kind: KtruNodeKind;
  status: string | null;
  isTemplate: boolean;
  childCount: number;
  hasChildren: boolean;
}

export interface KtruChildrenPage {
  parentId: string | null;
  nodes: KtruTreeNode[];
  total: number;
  offset: number;
  hasMore: boolean;
  filterMatches: number | null;
}
