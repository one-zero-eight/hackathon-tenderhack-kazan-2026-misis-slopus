import { resolve } from "node:path";

export const searchDatabasePath = () => resolve(process.env.SEARCH_DATABASE ?? "data/search-index.sqlite");
export const catalogDatabasePath = () => resolve(process.env.CATALOG_DATABASE ?? "data/catalog.sqlite");

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

export const ktruParquetPath = () => resolve(process.env.KTRU_PARQUET ?? "../data/ktru-actual.parquet");
export const moyKtruParquetPath = () => resolve(process.env.MOY_KTRU_PARQUET ?? "../data/moy-zakupki-ktru.parquet");
export const ktruSqlitePath = () => resolve(process.env.KTRU_SQLITE ?? "../data/ktru-actual.sqlite");
export const classifiersSqlitePath = () => resolve(process.env.CLASSIFIERS_SQLITE ?? "../data/classifiers.sqlite");
export const okpd2ParquetPath = () => resolve(process.env.OKPD2_PARQUET ?? "../data/okpd2-moy-zakupki.parquet");
export const kpgzParquetPath = () => resolve(process.env.KPGZ_PARQUET ?? "../data/pp-kpgz.parquet");
export const spgzParquetPath = () => resolve(process.env.SPGZ_PARQUET ?? "../data/spgz.parquet");
export const okeiParquetPath = () => resolve(process.env.OKEI_PARQUET ?? "../data/okei.parquet");