from __future__ import annotations

import threading
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import pyarrow.parquet as pq

from search_api.category_taxonomy import (
    UNRESOLVED_CATEGORY_ID,
    CategoryTaxonomy,
    _display_name,
    build_kpgz_taxonomy,
    load_kpgz_reference,
)
from search_api.config import DATA, settings


@dataclass(frozen=True, slots=True)
class Product:
    id: int
    name: str
    description: str
    category: str
    category_id: int
    # Portal STE leaf name — kept for embedding_document stability / sku_embeddings.
    portal_category: str
    manufacturer: str
    country: str
    unit: str
    part_number: str
    model_name: str
    okpd2: str
    okpd2_name: str
    kpgz_code: str
    kpgz_name: str
    spgz_id: int | None
    price: float | None
    image_ids: list[int]
    characteristics: list[dict[str, str]]
    source_updated_at: str


_COLUMNS = [
    "id", "name", "description", "manufacturer", "okei", "oksm",
    "okpd2_code", "okpd2_name", "production_code", "production_name",
    "category_id", "category", "category_path", "model", "barcode",
    "median_price", "reference_price", "cost_per_unit",
    "sku_image_ids", "characteristics", "created_date",
]

_lock = threading.Lock()
_fingerprint: tuple[tuple[str, float], ...] | None = None
_by_id: dict[int, Product] | None = None
_image_ids: set[int] | None = None
_category_stats: list[dict] | None = None
_taxonomy: CategoryTaxonomy | None = None


def _ktru_from_characteristics(chars: list[dict[str, str]]) -> str:
    for item in chars:
        if item["name"].casefold() == "ктру" and item["value"].strip():
            return item["value"].strip()
    return ""


def _row_to_product(
    row: dict,
    *,
    category_id: int,
    category_name: str,
    portal_category: str,
    kpgz_code: str,
    kpgz_name: str,
) -> Product:
    chars = []
    for item in row.get("characteristics") or []:
        if not item:
            continue
        name = (item.get("name") or "").strip()
        value = item.get("value")
        if not name or value is None or value == "":
            continue
        chars.append({"name": name, "value": str(value), "unit": (item.get("unit") or "").strip()})
    price = next(
        (p for p in (row.get("reference_price"), row.get("median_price"), row.get("cost_per_unit"))
         if isinstance(p, (int, float)) and p == p and p > 0),
        None,
    )
    images = [int(i) for i in (row.get("sku_image_ids") or []) if isinstance(i, (int, float)) and int(i) > 0]
    return Product(
        id=int(row["id"]),
        name=(row.get("name") or "").strip(),
        description=(row.get("description") or "").strip(),
        category=category_name,
        category_id=category_id,
        portal_category=portal_category,
        manufacturer=(row.get("manufacturer") or "").strip(),
        country=(row.get("oksm") or "").strip(),
        unit=(row.get("okei") or "").strip(),
        part_number=(row.get("barcode") or "").strip(),
        model_name=(row.get("model") or "").strip(),
        okpd2=(row.get("okpd2_code") or "").strip(),
        okpd2_name=(row.get("okpd2_name") or "").strip(),
        kpgz_code=kpgz_code,
        kpgz_name=kpgz_name,
        spgz_id=None,
        price=float(price) if price is not None else None,
        image_ids=list(dict.fromkeys(images)),
        characteristics=chars,
        source_updated_at=row.get("created_date") or "",
    )


def _kpgz_path() -> Path:
    return DATA / "pp-kpgz.parquet"


def _paths_fingerprint(paths: list[Path]) -> tuple[tuple[str, float], ...]:
    return tuple((str(path.resolve()), path.stat().st_mtime) for path in [*paths, _kpgz_path()])


def _load(paths: list[Path]) -> None:
    global _fingerprint, _by_id, _image_ids, _category_stats, _taxonomy
    reference = load_kpgz_reference(_kpgz_path())
    code_to_id = {row["code"]: row["id"] for row in reference}
    code_to_name = {row["code"]: row["name"] for row in reference}
    products: dict[int, Product] = {}
    images: set[int] = set()
    direct_counts: Counter[str] = Counter()
    for path in paths:
        parquet = pq.ParquetFile(path)
        for batch in parquet.iter_batches(batch_size=5000, columns=_COLUMNS):
            for row in batch.to_pylist():
                product_id = int(row["id"] or 0)
                name = (row.get("name") or "").strip()
                if product_id <= 0 or not name:
                    continue
                kpgz_code = (row.get("production_code") or "").strip()
                kpgz_name = (row.get("production_name") or "").strip() or code_to_name.get(kpgz_code, "")
                portal_category = (row.get("category") or "").strip() or "Без категории"
                if kpgz_code in code_to_id:
                    category_id = code_to_id[kpgz_code]
                    category_name = _display_name(kpgz_code, kpgz_name or code_to_name[kpgz_code])
                else:
                    category_id = UNRESOLVED_CATEGORY_ID
                    category_name = kpgz_name or "Без кода КПГЗ"
                if product_id in products:
                    continue
                product = _row_to_product(
                    row,
                    category_id=category_id,
                    category_name=category_name,
                    portal_category=portal_category,
                    kpgz_code=kpgz_code,
                    kpgz_name=kpgz_name,
                )
                products[product_id] = product
                images.update(product.image_ids)
                direct_counts[kpgz_code] += 1
    taxonomy = build_kpgz_taxonomy(reference, dict(direct_counts))
    names = {node["id"]: node["name"] for node in taxonomy.tree}
    leaf_counts: Counter[int] = Counter()
    for code, count in direct_counts.items():
        leaf_counts[code_to_id[code] if code in code_to_id else UNRESOLVED_CATEGORY_ID] += count
    stats = [
        {"id": cid, "name": names.get(cid, f"КПГЗ #{cid}"), "count": count}
        for cid, count in sorted(leaf_counts.items(), key=lambda item: (-item[1], names.get(item[0], ""), item[0]))
    ]
    # Publish a complete snapshot only after validation and construction succeed.
    _by_id, _image_ids, _category_stats, _taxonomy = products, images, stats, taxonomy
    _fingerprint = _paths_fingerprint(paths)


def reset_catalog() -> None:
    global _fingerprint, _by_id, _image_ids, _category_stats, _taxonomy
    with _lock:
        _fingerprint = None
        _by_id = None
        _image_ids = None
        _category_stats = None
        _taxonomy = None


def catalog_paths() -> list[Path]:
    paths = settings.catalog_parquet_paths()
    missing = [path for path in [*paths, _kpgz_path()] if not path.exists()]
    if missing:
        raise FileNotFoundError("Не найдены parquet каталога: " + ", ".join(str(p) for p in missing))
    return paths


def get_catalog(paths: list[Path] | None = None):
    resolved = paths or catalog_paths()
    fingerprint = _paths_fingerprint(resolved)
    with _lock:
        if _by_id is None or _fingerprint != fingerprint:
            _load(resolved)
        return _by_id, _image_ids, _category_stats


def product_count() -> int:
    by_id, *_ = get_catalog()
    return len(by_id)


def category_stats() -> list[dict]:
    *_, cats = get_catalog()
    return cats


def category_tree() -> list[dict]:
    get_catalog()
    assert _taxonomy is not None
    return _taxonomy.tree


def category_diagnostics() -> dict:
    get_catalog()
    assert _taxonomy is not None
    return _taxonomy.diagnostics()


def resolve_category_ids(category_id: int | None) -> frozenset[int] | None:
    if category_id is None:
        return None
    get_catalog()
    assert _taxonomy is not None
    return _taxonomy.descendants.get(category_id, frozenset({category_id}))


def has_image_id(image_id: int) -> bool:
    _, images, *_ = get_catalog()
    return image_id in images


def find_product(product_id: int) -> Product | None:
    if not isinstance(product_id, int) or product_id <= 0:
        return None
    by_id, *_ = get_catalog()
    return by_id.get(product_id)


def hydrate(ids: list[int]) -> list[Product]:
    by_id, *_ = get_catalog()
    return [by_id[i] for i in ids if i in by_id]


def list_products(*, category_id: int | None, offset: int, limit: int) -> tuple[list[Product], int]:
    by_id, *_ = get_catalog()
    allowed = resolve_category_ids(category_id)
    ids = sorted(by_id) if allowed is None else sorted(
        pid for pid, product in by_id.items() if product.category_id in allowed
    )
    return [by_id[i] for i in ids[offset: offset + limit]], len(ids)


def product_to_dict(product: Product) -> dict:
    ktru_code = _ktru_from_characteristics(product.characteristics)
    return {
        "id": product.id,
        "name": product.name,
        "description": product.description,
        "category": product.category,
        "categoryId": product.category_id,
        "manufacturer": product.manufacturer,
        "country": product.country,
        "unit": product.unit,
        "partNumber": product.part_number,
        "modelName": product.model_name,
        "okpd2": product.okpd2,
        "okpd2Name": product.okpd2_name,
        "ktruCode": ktru_code,
        "ktruName": "",
        "kpgzCode": product.kpgz_code,
        "kpgzName": product.kpgz_name,
        "spgzId": product.spgz_id,
        "price": product.price,
        "imageIds": product.image_ids,
        "characteristics": product.characteristics,
        "sourceUpdatedAt": product.source_updated_at,
    }
