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
