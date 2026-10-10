from __future__ import annotations

import hashlib

from search_api.ste_catalog import Product

MAX_CHARACTERISTICS = 24
MAX_CHAR_VALUE_LEN = 120
DOCUMENT_VERSION = "document-v2"


def _format_characteristic(item: dict[str, str]) -> str:
    value = item["value"]
    if len(value) > MAX_CHAR_VALUE_LEN:
        value = value[: MAX_CHAR_VALUE_LEN - 1] + "…"
    unit = f" {item['unit']}" if item.get("unit") else ""
    return f"{item['name']}: {value}{unit}"


def embedding_document(product: Product) -> str:
    chars = product.characteristics[:MAX_CHARACTERISTICS]
    content_parts = [
        f"Категория: {product.category}",
        f"Производитель: {product.manufacturer}" if product.manufacturer else "",
        f"Артикул: {product.part_number}" if product.part_number else "",
        f"ОКПД2: {product.okpd2}" if product.okpd2 else "",
        *[_format_characteristic(c) for c in chars],
        product.description[:2000],
    ]
    content = "\n".join(part for part in content_parts if part)
    return f"title: {product.name} | text: {content}"


def document_hash(document: str) -> str:
    prefix = f"embeddinggemma-2:768:search-result:{DOCUMENT_VERSION}\0".encode()
    return hashlib.sha256(prefix + document.encode("utf-8")).hexdigest()
