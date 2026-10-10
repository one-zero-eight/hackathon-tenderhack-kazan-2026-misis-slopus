import { query } from "@solidjs/router";
import { apiCatalogStats, apiGetProduct, apiGetSimilarProducts, apiSearchProducts } from "./api";
import type { DirectoryTab, SearchInput } from "./types";

export const getCatalogStats = query(async () => apiCatalogStats(), "catalog-stats");

export const searchProducts = query(async (input: SearchInput) => apiSearchProducts(input), "search-products");

export const getProduct = query(async (id: number) => {
  try {
    return await apiGetProduct(id);
  } catch {
    return null;
  }
}, "product-detail");

export const getSimilarProducts = query(async (id: number) => apiGetSimilarProducts(id), "similar-products");

export const getKtruStats = query(async () => {
  "use server";
  const { ktruStats } = await import("./server/ktru-tree");
  return ktruStats();
}, "ktru-stats");

export const getKtruChildren = query(async (parentId: string | null = null, offset = 0, filter = "") => {
  "use server";
  const { listKtruChildren } = await import("./server/ktru-tree");
  return listKtruChildren(parentId, offset, filter);
}, "ktru-children-v2");

export const getKtruRecord = query(async (code: string) => {
  "use server";
  const { getKtruRecord: load } = await import("./server/ktru-tree");
  const { reverseLinksFor } = await import("./server/classifiers");
  const record = load(code);
  if (!record) return null;
  const reverse = reverseLinksFor("ktru", code, record.okpd2?.code ?? "");
  const links = [
    ...(record.okpd2
      ? [{ tab: "okpd" as const, code: record.okpd2.code, label: `ОКПД2 ${record.okpd2.code}${record.okpd2.name ? ` — ${record.okpd2.name}` : ""}` }]
      : []),
    ...(record.parent
      ? [{ tab: "ktru" as const, code: record.parent.code, label: `КТРУ (родитель) ${record.parent.code}${record.parent.version ? ` · v${record.parent.version}` : ""}` }]
      : []),
    ...record.units.map(unit => ({ tab: "okei" as const, code: unit.code, label: `ОКЕИ ${unit.code} — ${unit.name}` })),
  ];
  return { ...record, links, ...reverse };
}, "ktru-record");

export const getKtruRaw = query(async (code: string) => {
  "use server";
  const { getKtruRaw: load } = await import("./server/ktru-tree");
  return load(code);
}, "ktru-raw");

export const getKtruFilterMeta = query(async (filter: string) => {
  "use server";
  const { ktruFilterMeta } = await import("./server/ktru-tree");
  return ktruFilterMeta(filter);
}, "ktru-filter-meta");

export const getKtruNode = query(async (code: string) => {
  "use server";
  const { getKtruNodeByCode } = await import("./server/ktru-tree");
  return getKtruNodeByCode(code);
}, "ktru-node");

export const getKtruRevealPath = query(async (code: string) => {
  "use server";
  const { getKtruRevealPath: load } = await import("./server/ktru-tree");
  return load(code);
}, "ktru-reveal-path");

export const getCatalogSpecialization = query(async (input: {
  ktruCode?: string | null;
  spgzCode?: string | null;
  kpgzCode?: string | null;
}) => {
  "use server";
  const { getCatalogSpecialization: load } = await import("./server/classifiers");
  return load(input);
}, "catalog-specialization");

export const getClassifierCharacteristicNames = query(async (input: {
  ktruCode?: string | null;
  okpd2?: string | null;
  spgzCode?: string | null;
  kpgzCode?: string | null;
}) => {
  "use server";
  const { classifierCharacteristicNames } = await import("./server/classifiers");
  return classifierCharacteristicNames(input);
}, "classifier-characteristic-names");

export const resolveProductCategoryVotes = query(async (input: {
  okpd2?: string | null;
  okpd2Name?: string | null;
  ktruCode?: string | null;
  ktruName?: string | null;
  kpgzCode?: string | null;
  kpgzName?: string | null;
  spgzId?: number | null;
  characteristics?: { name: string; value: string }[] | null;
}) => {
  "use server";
  const { resolveProductCategoryVotes: load } = await import("./server/classifiers");
  return load(input);
}, "product-category-votes");

export const getClassifierStats = query(async () => {
  "use server";
  const { classifierStats } = await import("./server/classifiers");
  return classifierStats();
}, "classifier-stats");

export const getDirectoryChildren = query(async (tab: DirectoryTab, parentId: string | null = null, offset = 0, filter = "") => {
  "use server";
  const { listDirectoryChildren } = await import("./server/classifiers");
  return listDirectoryChildren(tab, parentId, offset, filter);
}, "directory-children");

export const getDirectoryRecord = query(async (tab: DirectoryTab, code: string) => {
  "use server";
  const { getDirectoryRecord: load } = await import("./server/classifiers");
  return load(tab, code);
}, "directory-record");

export const getDirectoryRaw = query(async (tab: DirectoryTab, code: string) => {
  "use server";
  const { getDirectoryRaw: load } = await import("./server/classifiers");
  return load(tab, code);
}, "directory-raw");

export const getDirectoryNode = query(async (tab: DirectoryTab, code: string) => {
  "use server";
  const { getDirectoryNode: load } = await import("./server/classifiers");
  return load(tab, code);
}, "directory-node");

export const getDirectoryRevealPath = query(async (tab: DirectoryTab, code: string) => {
  "use server";
  const { getDirectoryRevealPath: load } = await import("./server/classifiers");
  return load(tab, code);
}, "directory-reveal-path");

export const getDirectoryFilterMeta = query(async (tab: DirectoryTab, filter: string) => {
  "use server";
  const { directoryFilterMeta } = await import("./server/classifiers");
  return directoryFilterMeta(tab, filter);
}, "directory-filter-meta");

export const getDirectoryProducts = query(async (tab: DirectoryTab, code: string) => {
  "use server";
  const { listDirectoryProducts } = await import("./server/classifiers");
  return listDirectoryProducts(tab, code);
}, "directory-products");

export const searchKtru = query(async (queryText: string) => {
  "use server";
  const { searchKtru: search } = await import("./server/ktru-tree");
  return search(queryText);
}, "ktru-search");

export const suggestFromNeighbors = query(async (description: string) => {
  "use server";
  const { suggestFromNeighbors: suggest } = await import("./server/neighbor-suggest");
  return suggest(description);
}, "neighbor-suggest");
