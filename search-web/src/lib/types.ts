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
  /** Разделяющие характеристики варианта шаблона, например «Материал: Дерево · Размер: 10». */
  variantLabel?: string | null;
  /** How the code was proposed: product neighbors, classifier search, or both. */
  source?: "neighbors" | "classifier" | "both";
};

export type ClassifierEntryHit = {
  id: number;
  kind: "okpd" | "ktru" | "kpgz" | "spgz";
  code: string;
  name: string;
  linkCode: string | null;
  status: string | null;
  isTemplate: boolean;
  isExcluded: boolean;
  score: number | null;
  match: string;
  evidence: string;
  synonyms: string[];
  weakSynonyms: string[];
  rank?: number;
};

export type ClassifierPairHit = {
  okpd2?: ClassifierEntryHit | null;
  ktru?: ClassifierEntryHit | null;
  kpgz?: ClassifierEntryHit | null;
  spgz?: ClassifierEntryHit | null;
  rank: number;
  evidence: string;
  sources: string[];
  ambiguous: boolean;
  confident: boolean;
  needsChoice: boolean;
  reason: string | null;
};

export type ClassifierSearchResult = {
  query: string;
  mode: SearchMode;
  okpdKtru: ClassifierPairHit[];
  kpgzSpgz: ClassifierPairHit[];
  hits: ClassifierEntryHit[];
  needsChoice: boolean;
  durationMs: number;
};

export type CategoryChoiceReason =
  | "branch_conflict"
  | "ambiguous_classifier"
  | "weak_neighbors"
  | "multiple_candidates";

export type CategoryPairCandidate = {
  okpd2: VotedCode | null;
  ktru: VotedCode | null;
  kpgz: VotedCode | null;
  spgz: VotedCode | null;
  sources: ("neighbors" | "classifier")[];
  reason: string | null;
};

export type CategoryPairDecision = {
  needsChoice: boolean;
  reason: string | null;
  alternatives: CategoryPairCandidate[];
};

export type CharacteristicRange = {
  option: string;
  min: number | null;
  max: number | null;
  minInclusive: boolean;
  maxInclusive: boolean;
};

export type AutofillFieldStatus = "accepted" | "rejected" | "missing";

export type AutofillEvidence = {
  quote: string;
  start: number;
  end: number;
  source: "llm" | "rules";
  conversion?: { from: string; to: string; factor: number | null; result: number } | null;
};

export type AutofillDiagnostics = {
  status: AutofillFieldStatus;
  confidence?: number | null;
  quote?: string | null;
  errors?: string[];
  conversion?: { from: string; to: string; result: number; sourceValue: number; factor?: number | null } | null;
  suggestion?: { value: number; unit: string; converted: number; reason: string } | null;
  evidence?: AutofillEvidence | null;
  model?: string;
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
  source: "ktru" | "spgz" | "both" | "catalog" | "custom";
  catalogFrequency?: CatalogCharacteristic;
  /** Missing from the selected child position, retained from this KTRU template. */
  templateKtruCode?: string;
  /** Confidence for the current model-filled value; removed on manual edits. */
  autofill?: { confidence: number; model: string; evidence?: AutofillEvidence | null };
  /** Last autofill diagnostics for this field, independent of the editable value. */
  autofillDiagnostics?: AutofillDiagnostics;
  custom?: boolean;
  /** Choice partitions siblings and sets refined KTRU / СПГЗ. */
  refinesKtru?: boolean;
  /** When false, value is kept but does not switch KTRU / СПГЗ. Default true. */
  applyPositionRefine?: boolean;
};

export type CatalogCharacteristic = {
  id: string;
  name: string;
  /** Morphologically normalized backend identity; display name remains readable. */
  nameKey?: string;
  unit: string;
  numeric: boolean;
  count: number;
  total: number;
  frequency: number;
  scope: "category" | "adjacent" | "schema";
  examples: string[];
  /** Products with each normalized example, counted once per product. */
  exampleCounts: Record<string, number>;
  /** Complete numeric distribution for single-valued products; null when aggregation is unsafe. */
  numericValueCounts: Record<string, number> | null;
};

export type CharacteristicAutofillInput = {
  text: string;
  characteristics: (Pick<DraftCharacteristic, "id" | "name" | "options" | "optionLabels" | "ranges" | "unit" | "numeric" | "multiple" | "required" | "hint" | "source"> & { examples?: string[] })[];
  standards: string[];
  ktruCode?: string | null;
  additionalAllowed?: boolean | null;
  excludedNames?: string[];
  webFacts?: string[];
};

export type WebEnrichmentSuggestion = {
  brand: string;
  model: string;
  query: string;
  sources: { title: string; url: string; snippet: string }[];
  facts: string[];
  suggestedDescription: string;
  message: string;
};

export type CharacteristicAutofillResult = {
  status: "accepted" | "needs_review" | "no_match";
  characteristics: {
    id: string;
    name?: string;
    value: string | null;
    rangeOption: string;
    status: AutofillFieldStatus;
    confidence: number | null;
    quote: string | null;
    errors: string[];
    source?: string | null;
    conversion?: AutofillDiagnostics["conversion"];
    suggestion?: AutofillDiagnostics["suggestion"];
    evidence?: AutofillEvidence | null;
  }[];
  additional_fields: {
    name: string;
    value: string | number | null;
    unit: string | null;
    quote: string | null;
    status: AutofillFieldStatus;
    confidence: number | null;
    errors: string[];
    evidence?: AutofillEvidence | null;
    conversion?: AutofillDiagnostics["conversion"];
  }[];
  additional_allowed: boolean | null;
  product: Record<"name" | "model" | "manufacturer" | "article", {
    name: string;
    value: string | null;
    quote: string | null;
    status: AutofillFieldStatus;
    source: string | null;
    errors: string[];
    evidence?: AutofillEvidence | null;
  }>;
  webEnrichment?: WebEnrichmentSuggestion | null;
  issues: string[];
  threshold: number;
  filled: number;
  total: number;
  model: string;
  jevModel?: string;
  durationMs: number;
  usage: {
    input_tokens: number;
    output_tokens: number;
    llm?: { input_tokens: number; output_tokens: number };
    jev?: { input_tokens: number; output_tokens: number };
  };
};

export type SpecializationCharacteristic = {
  name: string;
  code: string | null;
};

/** One sibling position keyed by values of every discriminating characteristic. */
export type KtruSpecializationOption = {
  values: Record<string, string>;
  code: string;
  name: string;
};

export type KtruSpecialization = {
  templateCode: string;
  templateName: string;
  characteristics: SpecializationCharacteristic[];
  options: KtruSpecializationOption[];
  currentValues: Record<string, string> | null;
};

export type CatalogSpecializationOption = {
  values: Record<string, string>;
  ktruCode: string | null;
  ktruName: string | null;
  spgzCode: string | null;
  spgzName: string | null;
};

export type CatalogSpecialization = {
  characteristics: SpecializationCharacteristic[];
  templateKtruCode: string | null;
  templateKtruName: string | null;
  options: CatalogSpecializationOption[];
  currentValues: Record<string, string> | null;
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
  /** Разделяющие характеристики варианта шаблона, например «Материал: Дерево · Размер: 10». */
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
