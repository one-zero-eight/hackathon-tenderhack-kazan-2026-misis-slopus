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

export type VotedCode = {
  code: string;
  name: string;
  score: number;
  isTemplate?: boolean;
  /** Разделяющая характеристика варианта шаблона, например «Материал: Дерево». */
  variantLabel?: string | null;
};

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
  /** Quantitative schema field, including SPGZ fields without a declared unit. */
  numeric?: boolean;
  multiple: boolean;
  source: "ktru" | "spgz" | "both" | "custom";
  /** Missing from the selected child position, retained from this KTRU template. */
  templateKtruCode?: string;
  /** Confidence for the current model-filled value; removed on manual edits. */
  autofill?: { confidence: number; model: string };
  /** Model estimates for schema options, independent of the manually selected value. */
  optionProbabilities?: { text: string; values: Record<string, number> };
  custom?: boolean;
  /** Choice partitions siblings and sets refined KTRU / СПГЗ. */
  refinesKtru?: boolean;
  /** When false, value is kept but does not switch KTRU / СПГЗ. Default true. */
  applyPositionRefine?: boolean;
};

export type CharacteristicAutofillInput = {
  text: string;
  characteristics: Pick<DraftCharacteristic, "id" | "name" | "options" | "optionLabels" | "ranges" | "unit" | "numeric" | "multiple" | "hint">[];
  standards: string[];
};

export type CharacteristicAutofillResult = {
  characteristics: { id: string; value: string; rangeOption: string; confidence: number; evidence: string }[];
  optionProbabilities: Record<string, Record<string, number>>;
  model: string;
  durationMs: number;
  usage: { input_tokens: number; output_tokens: number };
};

export type KtruSpecializationOption = { value: string; code: string; name: string };

export type KtruSpecialization = {
  templateCode: string;
  templateName: string;
  characteristicName: string;
  characteristicCode: string | null;
  options: KtruSpecializationOption[];
  currentValue: string | null;
};

export type CatalogSpecializationOption = {
  value: string;
  ktruCode: string | null;
  ktruName: string | null;
  spgzCode: string | null;
  spgzName: string | null;
};

export type CatalogSpecialization = {
  characteristicName: string;
  characteristicCode: string | null;
  templateKtruCode: string | null;
  templateKtruName: string | null;
  options: CatalogSpecializationOption[];
  currentValue: string | null;
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
  /** Разделяющая характеристика варианта шаблона, например «Материал: Дерево». */
  variantLabel?: string | null;
}

export interface KtruChildrenPage {
  parentId: string | null;
  nodes: KtruTreeNode[];
  total: number;
  offset: number;
  hasMore: boolean;
  filterMatches: number | null;
}
