import { apiSearchProducts } from "./search-api";
import { isRangeCharacteristic } from "../characteristic-options";
import { draftFromSpgzCharacteristic } from "../spgz-characteristics";
import type { CatalogSpecialization, DraftCharacteristic, KtruTreeNode, ProductHit, VotedCode } from "../types";
import { getCatalogSpecialization, getDirectoryRecord, getSoleSpgzForKpgz } from "./classifiers";
import { getKtruNodeByCode, getKtruRecord, getSoleTemplateKtruForOkpd, type KtruCharacteristic } from "./ktru-tree";

const NEIGHBOR_K = 25;
const SHOW_NEIGHBORS = 6;
const VOTE_TEMPERATURE = 0.05;

export type { DraftCharacteristic, VotedCode };

export type NeighborSuggestion = {
  neighbors: ProductHit[];
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
};

function ktruFromProduct(product: ProductHit): string {
  if (product.ktruCode?.trim()) return product.ktruCode.trim();
  for (const item of product.characteristics ?? []) {
    if (item.name.trim().toLocaleLowerCase("ru") === "ктру" && item.value.trim()) return item.value.trim();
  }
  return "";
}

function voteCodes(rows: { code: string; name: string; score: number }[]): VotedCode[] {
  if (!rows.length) return [];
  const top = Math.max(...rows.map((row) => row.score));
  const weights = new Map<string, { weight: number; name: string }>();
  for (const row of rows) {
    if (!row.code) continue;
    const add = Math.exp((row.score - top) / VOTE_TEMPERATURE);
    const current = weights.get(row.code) ?? { weight: 0, name: row.name || row.code };
    current.weight += add;
    if (row.name) current.name = row.name;
    weights.set(row.code, current);
  }
  const total = [...weights.values()].reduce((sum, item) => sum + item.weight, 0) || 1;
  return [...weights.entries()]
    .map(([code, item]) => ({ code, name: item.name, score: item.weight / total }))
    .sort((a, b) => b.score - a.score || a.code.localeCompare(b.code, "ru"));
}

function fillCharacteristics(schema: KtruCharacteristic[]): DraftCharacteristic[] {
  return schema.map((item) => {
    const options = item.options ?? [];
    const ranges = item.ranges ?? [];
    const id = item.code || item.name;
    return {
      id,
      name: item.name,
      value: "",
      rangeOption: "",
      required: item.required,
      badges: item.badges.filter((badge) =>
        !/^(обязательная|качественная|количественная)$/i.test(badge.trim())),
      hint: item.hint || "",
      options,
      optionLabels: item.optionLabels ?? {},
      ranges: isRangeCharacteristic(item) ? ranges : [],
      unit: item.unit || "",
      multiple: !!item.multiple,
      source: "ktru" as const,
    };
  });
}

function nameFromDescription(text: string): string {
  return text.trim().split(/[.!?\n]/)[0]?.trim().slice(0, 200) ?? "";
}

function enrichName(vote: VotedCode | null, tab: "okpd" | "kpgz" | "spgz"): VotedCode | null {
  if (!vote) return null;
  if (vote.name && vote.name !== vote.code) return vote;
  const record = getDirectoryRecord(tab, vote.code);
  return record?.name ? { ...vote, name: record.name } : vote;
}

function resolveSpgz(neighbors: ProductHit[], kpgz: VotedCode | null): VotedCode | null {
  const fromNeighbors = voteCodes(
    neighbors.flatMap((product) => {
      if (!product.spgzId || (kpgz && product.kpgzCode !== kpgz.code)) return [];
      return [{
        code: String(product.spgzId),
        name: String(product.spgzId),
        score: typeof product.score === "number" ? product.score : 0,
      }];
    }),
  )[0] ?? null;
  if (fromNeighbors) return enrichName(fromNeighbors, "spgz");
  const sole = kpgz ? getSoleSpgzForKpgz(kpgz.code) : null;
  return sole && kpgz ? { ...sole, score: kpgz.score } : null;
}

function resolveKtruFromVotes(neighbors: ProductHit[], okpd2: VotedCode | null): {
  vote: VotedCode | null;
  status: string | null;
  characteristics: KtruCharacteristic[];
  standards: string[];
  noNewFeatures: boolean | null;
  noNewFeaturesReason: string;
  okpdFromKtru: { code: string; name: string } | null;
  node: KtruTreeNode | null;
} {
  let best = voteCodes(
    neighbors.flatMap((product) => {
      const code = ktruFromProduct(product);
      if (!code) return [];
      return [{
        code,
        name: product.ktruName || code,
        score: typeof product.score === "number" ? product.score : 0,
      }];
    }),
  )[0] ?? null;

  // No KTRU on STE neighbors → only auto-pick when OKPD2 has a sole active template root.
  if (!best && okpd2) {
    const sole = getSoleTemplateKtruForOkpd(okpd2.code);
    if (sole) best = { code: sole.code, name: sole.name, score: okpd2.score };
  }

  if (!best) {
    return {
      vote: null,
      status: null,
      characteristics: [],
      standards: [],
      noNewFeatures: null,
      noNewFeaturesReason: "",
      okpdFromKtru: null,
      node: null,
    };
  }

  const record = getKtruRecord(best.code);
  const node = getKtruNodeByCode(best.code);
  return {
    vote: {
      code: best.code,
      name: record?.name || best.name,
      score: best.score,
      isTemplate: !!(node?.isTemplate ?? record?.isTemplate),
      variantLabel: node?.variantLabel ?? null,
    },
    node,
    status: node?.status ?? record?.status ?? null,
    characteristics: record?.characteristics ?? [],
    standards: record?.standards ?? [],
    noNewFeatures: record ? record.noNewFeatures : null,
    noNewFeaturesReason: record?.noNewFeaturesReason ?? "",
    okpdFromKtru: record?.okpd2?.code
      ? { code: record.okpd2.code, name: record.okpd2.name || record.okpd2.code }
      : null,
  };
}

/** Соседи СТЕ без фильтрации → голос за ОКПД2 / КПГЗ / КТРУ → схема характеристик. */
export async function suggestFromNeighbors(description: string): Promise<NeighborSuggestion> {
  const query = description.trim().replace(/\s+/g, " ");
  if (query.length < 12) {
    return {
      neighbors: [],
      okpd2: null,
      kpgz: null,
      spgz: null,
      ktru: null,
      ktruStatus: null,
      characteristics: [],
      standards: [],
      noNewFeatures: null,
      noNewFeaturesReason: "",
      productName: "",
      specialization: null,
      ktruNode: null,
      templateCharacteristics: null,
    };
  }

  const result = await apiSearchProducts({ query, mode: "semantic", page: 1 });
  const neighbors = result.items.slice(0, NEIGHBOR_K).map((item) => ({
    ...item,
    score: typeof item.score === "number" ? item.score : 0,
    ktruCode: item.ktruCode || ktruFromProduct(item),
  }));
  const scored = neighbors.map((item) => ({
    item,
    score: typeof item.score === "number" ? item.score : 0,
  }));

  let okpd2 = enrichName(
    voteCodes(
      scored.flatMap(({ item, score }) => item.okpd2
        ? [{ code: item.okpd2, name: item.okpd2Name || item.okpd2, score }]
        : []),
    )[0] ?? null,
    "okpd",
  );

  const kpgz = enrichName(
    voteCodes(
      scored.flatMap(({ item, score }) => item.kpgzCode
        ? [{ code: item.kpgzCode, name: item.kpgzName || item.kpgzCode, score }]
        : []),
    )[0] ?? null,
    "kpgz",
  );

  const {
    vote: ktru,
    status,
    characteristics: schema,
    standards,
    noNewFeatures,
    noNewFeaturesReason,
    okpdFromKtru,
    node: ktruNode,
  } = resolveKtruFromVotes(neighbors, okpd2);
  if (okpdFromKtru && !okpd2) {
    okpd2 = enrichName({ code: okpdFromKtru.code, name: okpdFromKtru.name, score: ktru?.score ?? 1 }, "okpd");
  }

  const spgz = resolveSpgz(neighbors, kpgz);
  const ktruCharacteristics = fillCharacteristics(schema);
  const spgzCharacteristics = (() => {
    if (!spgz) return [] as DraftCharacteristic[];
    const record = getDirectoryRecord("spgz", spgz.code);
    return (record?.characteristics ?? []).map((item) => draftFromSpgzCharacteristic(item));
  })();
  const characteristics = [...ktruCharacteristics, ...spgzCharacteristics];
  const productName = nameFromDescription(query) || ktru?.name || neighbors[0]?.name || "";
  const specialization = getCatalogSpecialization({
    ktruCode: ktru?.code,
    spgzCode: spgz?.code,
    kpgzCode: kpgz?.code,
  });
  const templateCode = specialization?.templateKtruCode;
  const templateCharacteristics = templateCode && ktru && templateCode !== ktru.code
    ? getKtruRecord(templateCode)?.characteristics ?? null
    : null;

  return {
    neighbors: neighbors.slice(0, SHOW_NEIGHBORS),
    okpd2,
    kpgz,
    spgz,
    ktru,
    ktruStatus: status,
    characteristics,
    standards,
    noNewFeatures,
    noNewFeaturesReason,
    productName,
    specialization,
    ktruNode,
    templateCharacteristics,
  };
}
