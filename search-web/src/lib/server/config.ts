import { resolve } from "node:path";

export const MODEL_ID = "google/embeddinggemma-2";
export const DIMENSIONS = 768;
/** Query prefix for EmbeddingGemma SearchQuery (asymmetric retrieval). */
export const QUERY_PROMPT = "task: search result | query: ";
/** Document texts are fully formatted as `title: … | text: …`; no ST prompt prefix. */
export const DOCUMENT_PROMPT = "";
export const INDEX_SCHEMA_VERSION = "parquet-index-v1";
export { PAGE_SIZE, RETRIEVAL_LIMIT } from "../search-config";
export const searchDatabasePath = () => resolve(process.env.SEARCH_DATABASE ?? "data/search-index.sqlite");

const defaultCategoryParquets = [
  "../data/Инструменты_from_api.parquet",
  "../data/Мебель_from_api.parquet",
  "../data/Одежда_from_api.parquet",
];

/** Primary catalogs (Инструменты / Мебель / Одежда). Optional STE via STE_PARQUET. */
export const catalogParquetPaths = (): string[] => {
  const fromEnv = (process.env.CATEGORY_PARQUETS ?? "")
    .split(",")
    .map((part) => part.trim())
    .filter(Boolean)
    .map((part) => resolve(part));
  const paths = fromEnv.length ? fromEnv : defaultCategoryParquets.map((part) => resolve(part));
  const ste = (process.env.STE_PARQUET ?? process.env.STE_DETAILS_PARQUET ?? "").trim();
  if (ste) paths.push(resolve(ste));
  return paths;
};

/** @deprecated use catalogParquetPaths — kept for scripts that expect a single path */
export const steParquetPath = () => catalogParquetPaths()[0] ?? resolve("../data/ste.parquet");
export const ktruParquetPath = () => resolve(process.env.KTRU_PARQUET ?? "../data/ktru-actual.parquet");
export const ktruSqlitePath = () => resolve(process.env.KTRU_SQLITE ?? "../data/ktru-actual.sqlite");
export const classifiersSqlitePath = () => resolve(process.env.CLASSIFIERS_SQLITE ?? "../data/classifiers.sqlite");
export const okpd2ParquetPath = () => resolve(process.env.OKPD2_PARQUET ?? "../data/okpd2-moy-zakupki.parquet");
export const kpgzParquetPath = () => resolve(process.env.KPGZ_PARQUET ?? "../data/pp-kpgz.parquet");
export const spgzParquetPath = () => resolve(process.env.SPGZ_PARQUET ?? "../data/spgz.parquet");
export const okeiParquetPath = () => resolve(process.env.OKEI_PARQUET ?? "../data/okei.parquet");
export const meowEmbedUrl = () => process.env.MEOW_EMBED_URL ?? "https://api.innohassle.ru/meow-embed";
