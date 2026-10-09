import { index, integer, real, sqliteTable, text } from "drizzle-orm/sqlite-core";
import type { Characteristic } from "../types";

export const products = sqliteTable("products", {
  id: integer("id").primaryKey(),
  name: text("name").notNull(),
  description: text("description").notNull(),
  category: text("category").notNull(),
  categoryId: integer("category_id").notNull(),
  manufacturer: text("manufacturer").notNull(),
  country: text("country").notNull(),
  unit: text("unit").notNull(),
  partNumber: text("part_number").notNull(),
  modelName: text("model_name").notNull().default(""),
  okpd2: text("okpd2").notNull(),
  okpd2Name: text("okpd2_name").notNull().default(""),
  ktruCode: text("ktru_code").notNull().default(""),
  ktruName: text("ktru_name").notNull().default(""),
  kpgzCode: text("kpgz_code").notNull().default(""),
  kpgzName: text("kpgz_name").notNull().default(""),
  spgzId: integer("spgz_id"),
  price: real("price"),
  imageIds: text("image_ids", { mode: "json" }).$type<number[]>().notNull(),
  characteristics: text("characteristics", { mode: "json" }).$type<Characteristic[]>().notNull(),
  sourceUpdatedAt: text("source_updated_at").notNull(),
  sourceFetchedAt: text("source_fetched_at").notNull(),
  document: text("document").notNull(),
  contentHash: text("content_hash").notNull(),
  embeddedHash: text("embedded_hash"),
}, (table) => [index("products_category_idx").on(table.categoryId), index("products_embedded_idx").on(table.embeddedHash)]);

export const metadata = sqliteTable("metadata", {
  key: text("key").primaryKey(),
  value: text("value").notNull(),
});
