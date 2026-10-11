import { apiSuggestCategories } from "./search-api";
import type {
  CatalogSpecialization,
  CategoryChoiceReason,
  CategoryPairDecision,
  DraftCharacteristic,
  KtruTreeNode,
  ProductHit,
  VotedCode,
} from "../types";
import type { KtruCharacteristic } from "./ktru-tree";

export type { DraftCharacteristic, VotedCode };

export type QueryQualityVerdict = "ok" | "suspicious" | "reject" | "unchecked";

export type QueryQuality = {
  verdict: QueryQualityVerdict;
  reasons: string[];
  probabilities?: Record<string, number>;
  durationMs?: number;
};

export type NeighborSuggestion = {
  neighbors: ProductHit[];
  /** Browser request time until the first neighbor results arrive, including quality checking. */
  neighborsDurationMs?: number;
  okpd2: VotedCode | null;
  kpgz: VotedCode | null;
  spgz: VotedCode | null;
  ktru: VotedCode | null;
  ktruStatus: string | null;
  characteristics: DraftCharacteristic[];
  standards: string[];
  noNewFeatures: boolean | null;
  noNewFeaturesReason: string;
  productName: string;
  specialization: CatalogSpecialization | null;
  ktruNode: KtruTreeNode | null;
  templateCharacteristics: KtruCharacteristic[] | null;
  okpdKtruDecision: CategoryPairDecision;
  kpgzSpgzDecision: CategoryPairDecision;
  needsChoice: boolean;
  choiceReason: CategoryChoiceReason | null;
  classifierError: string | null;
  neighborError: string | null;
  queryQuality: QueryQuality | null;
};

export type NeighborSuggestionProgress = Partial<Pick<NeighborSuggestion, "queryQuality" | "neighbors" | "neighborError" | "neighborsDurationMs">>;

/** Python owns retrieval, relevance filtering, pair voting and catalog schemas. */
export function suggestFromNeighbors(
  description: string,
  options: { force?: boolean } = {},
): Promise<NeighborSuggestion> {
  return apiSuggestCategories(description, options);
}
