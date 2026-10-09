CREATE TABLE `metadata` (
	`key` text PRIMARY KEY NOT NULL,
	`value` text NOT NULL
);
--> statement-breakpoint
CREATE TABLE `products` (
	`id` integer PRIMARY KEY NOT NULL,
	`name` text NOT NULL,
	`description` text NOT NULL,
	`category` text NOT NULL,
	`category_id` integer NOT NULL,
	`manufacturer` text NOT NULL,
	`country` text NOT NULL,
	`unit` text NOT NULL,
	`part_number` text NOT NULL,
	`okpd2` text NOT NULL,
	`price` real,
	`image_ids` text NOT NULL,
	`characteristics` text NOT NULL,
	`source_updated_at` text NOT NULL,
	`source_fetched_at` text NOT NULL,
	`document` text NOT NULL,
	`content_hash` text NOT NULL,
	`embedded_hash` text
);
--> statement-breakpoint
CREATE INDEX `products_category_idx` ON `products` (`category_id`);--> statement-breakpoint
CREATE INDEX `products_embedded_idx` ON `products` (`embedded_hash`);