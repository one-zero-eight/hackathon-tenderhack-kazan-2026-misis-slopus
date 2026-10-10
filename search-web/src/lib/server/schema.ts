import { integer, sqliteTable, text } from "drizzle-orm/sqlite-core";

/** Thin index sync state — display fields live in parquet. */
export const indexState = sqliteTable("index_state", {
  productId: integer("product_id").primaryKey(),
  contentHash: text("content_hash").notNull(),
  embeddedHash: text("embedded_hash"),
  categoryId: integer("category_id").notNull(),
});

export const metadata = sqliteTable("metadata", {
  key: text("key").primaryKey(),
  value: text("value").notNull(),
});
