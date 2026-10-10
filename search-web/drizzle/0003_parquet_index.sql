CREATE TABLE `index_state` (
	`product_id` integer PRIMARY KEY NOT NULL,
	`content_hash` text NOT NULL,
	`embedded_hash` text,
	`category_id` integer NOT NULL
);
