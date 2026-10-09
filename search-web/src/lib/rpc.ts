import { query } from "@solidjs/router";
import type { SearchInput } from "./types";

export const getCatalogStats = query(async () => {
  "use server";
  const { catalogStats } = await import("./server/search");
  return catalogStats();
}, "catalog-stats");

export const searchProducts = query(async (input: SearchInput) => {
  "use server";
  const { runSearch } = await import("./server/search");
  return runSearch(input);
}, "search-products");

export const getProduct = query(async (id: number) => {
  "use server";
  const { findProduct } = await import("./server/search");
  return findProduct(id);
}, "product-detail");

export const getSimilarProducts = query(async (id: number) => {
  "use server";
  const { findSimilarProducts } = await import("./server/search");
  return findSimilarProducts(id);
}, "similar-products");

export const getKtruStats = query(async () => {
  "use server";
  const { ktruStats } = await import("./server/ktru-tree");
  return ktruStats();
}, "ktru-stats");

export const getKtruChildren = query(async (parentId: string | null = null, offset = 0, filter = "") => {
  "use server";
  const { listKtruChildren } = await import("./server/ktru-tree");
  return listKtruChildren(parentId, offset, filter);
}, "ktru-children");

export const getKtruRecord = query(async (code: string) => {
  "use server";
  const { getKtruRecord: load } = await import("./server/ktru-tree");
  return load(code);
}, "ktru-record");

export const getKtruFilterMeta = query(async (filter: string) => {
  "use server";
  const { ktruFilterMeta } = await import("./server/ktru-tree");
  return ktruFilterMeta(filter);
}, "ktru-filter-meta");
