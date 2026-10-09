import { resolve } from "node:path";

export const MODEL_ID = "sergeyzh/BERTA";
export const DIMENSIONS = 768;
export { PAGE_SIZE, RETRIEVAL_LIMIT } from "../search-config";
export const searchDatabasePath = () => resolve(process.env.SEARCH_DATABASE ?? "data/search-berta.sqlite");
export const sourceDatabasePath = () => resolve(process.env.SOURCE_DATABASE ?? "../data/ste-details.sqlite");
export const meowEmbedUrl = () => process.env.MEOW_EMBED_URL ?? "http://127.0.0.1:8067";
