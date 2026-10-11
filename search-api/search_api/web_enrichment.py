from __future__ import annotations

import asyncio
import logging
import re
from collections import Counter
from typing import Any

logger = logging.getLogger("uvicorn.error")

# Product codes: GSB 550, a320, XR2000, IdeaPad 320, ThinkPad X1, Makita 6827
MODEL_RE = re.compile(
    r"(?<![A-Za-z0-9./])("
    r"[A-Z]{1,8}(?:[-\s]?[A-Z]{0,4})?[-\s]?\d{2,5}(?:[./-]?\d{0,4})?[A-Z]{0,4}"
    r"|[A-Za-z]{2,}\d{2,}[A-Za-z0-9./-]{0,8}"
    r"|[A-Za-z]\d{3,}[A-Za-z0-9./-]{0,6}"
    r"|[A-Z][A-Za-z]{2,}(?:Pad|Book|Phone|Tab)?[-\s]?\d{2,5}(?:[./-][A-Za-z0-9]+)?"
    r")(?![A-Za-z0-9./])"
)
# Pure numeric (or digit+letter) tool SKUs after a brand: Makita 6827, Bosch 06011A1023
NUMERIC_MODEL_RE = re.compile(
    r"(?<![A-Za-z0-9./])(\d{3,5}[A-Za-z]{0,3})(?![A-Za-z0-9./])"
)
YEAR_MODEL_RE = re.compile(r"^(?:19|20)\d{2}$")
EXPLICIT_MODEL_RE = re.compile(
    r"(?:модель|артикул|марка)\s*[:\-]?\s*([^\s,;()]{2,40})", re.I,
)
EXPLICIT_BRAND_RE = re.compile(
    r"(?:бренд|производитель|изготовитель)\s*[:\-]?\s*([^\s,;()]{2,40})", re.I,
)
LATIN_TOKEN_RE = re.compile(r"(?<![A-Za-z])([A-Za-z]{3,24})(?![A-Za-z])")
PRODUCT_KIND_PATTERNS = [
    (re.compile(r"(?i)\b(?:ноутбук\w*|notebook|laptop|ultrabook)\b"), "ноутбук"),
    (re.compile(r"(?i)\b(?:дрел\w*|drill)\b"), "дрель"),
    (re.compile(r"(?i)\b(?:шурупов[её]рт\w*|screwdriver)\b"), "шуруповёрт"),
    (re.compile(r"(?i)\b(?:смартфон\w*|телефон\w*|phone)\b"), "смартфон"),
    (re.compile(r"(?i)\b(?:монитор\w*|monitor)\b"), "монитор"),
    (re.compile(r"(?i)\b(?:принтер\w*|printer)\b"), "принтер"),
]
MEASURE_RE = re.compile(
    r"(?<![\w.,-])-?\d+(?:[.,]\d+)?(?:\s*[-–—]\s*-?\d+(?:[.,]\d+)?)?"
    r"(?:\s*[xх×*]\s*-?\d+(?:[.,]\d+)?)*"
    r"(?:\s*(?:мм|см|дм|м|кг|г|вт|квт|л|мл|%|шт|об/?мин|об\.?/?\s*мин|rpm|w|kw|"
    r"гб|gb|тб|tb|ггц|ghz|mhz|мп|mp|дюйм\w*|\"|″))(?!\w)",
    re.I,
)
CLAUSE_SPLIT_RE = re.compile(
    r"[;|\n•·]|(?<=[.!?])\s+|(?<=,)\s+(?=(?:intel|amd|озу|ram|ssd|hdd|вес|мощность|"
    r"процессор|память|диск|экран|диагональ|\d))",
    re.I,
)
NOISE_RE = re.compile(
    r"(?i)\b(?:купит\w*|корзин\w*|заказ\w*|клик\w*|консультац\w*|отзыв\w*|обзор\w*|"
    r"добавьте|оформите|просто|выгодн\w*|скидк\w*|доставк\w*|гаранти\w*|"
    r"everything about|designed to|handle any task|preloaded with|"
    r"по\s+низк\w*\s+цен\w*|бесплатн\w*\s+доставк\w*)\b"
)
MARKETING_RE = re.compile(
    r"(?i)\b(?:идеальн\w*|над[её]жн\w*|известн\w*|лучший\w*|уникальн\w*|"
    r"lifetime|perfect|ideal|reliable|powerful and|сбалансированн\w*)\b"
)
PRICE_RE = re.compile(r"(?i)\d[\d\s]{0,8}\s*(?:₽|руб\.?|р\.)")
ACCESSORY_RE = re.compile(
    r"(?i)\b(?:запчаст\w*|щетк\w*|угольн\w*\s*щетк\w*|ограничител\w*|глубин\w*\s*10|"
    r"количество\s+в\s+упаковке|для\s+бит|держател\w*\s*бит|комплект\w*\s*щёток|"
    r"артикул\s*запчаст\w*)\b"
)
LISTING_TITLE_RE = re.compile(
    r"(?i)^(?:смартфон|ноутбук|телефон|дрель|шуруповёрт|монитор|принтер)\b"
)
PHONE_COMBO_RE = re.compile(
    r"(?i)(?<![\d./])(\d{1,2})\s*/\s*(\d{2,4})\s*(?:гб|gb)\b"
)
MB_RAM_RE = re.compile(
    r"(?i)(?:^|[^\d])(2048|4096|8192|16384|32768)(?:\s*/|\s*(?:mb|мб)\b)"
)

# Structured extractors: (label, regex with captures of the value-ish span)
SPEC_EXTRACTORS: list[tuple[str, re.Pattern[str]]] = [
    ("Процессор", re.compile(
        r"(?i)((?:intel\s+)?core\s+i[3579](?:[-\s]?\d{3,4}\w*)?"
        r"|i[3579][-\s]?\d{3,4}\w*"
        r"|amd\s+(?:ryzen\s*\d+\w*|a(?:\d{1,2}|4|6|8|9|10|12)(?:-\d+)?\w*)"
        r"|amd\s+a(?:4|6|8|9|10|12)\b"
        r")(?:\s*[\d.,]+\s*(?:ггц|ghz))?"
    )),
    ("ОЗУ", re.compile(
        r"(?i)(?:озу|ram|память(?:\s*озу)?)\s*[:\-]?\s*(\d+\s*(?:гб|gb)(?:\s*ddr\d*)?)"
        r"|(\d+\s*(?:гб|gb)\s*(?:озу|ram|ddr\d*))"
        r"|(?<![\d./])(\d{1,2}\s*(?:гб|gb)(?:\s*ddr\d*)?)(?!\s*(?:ssd|hdd|nvme))"
    )),
    ("Накопитель", re.compile(
        r"(?i)(?:диск|ssd|hdd|накопитель|емкость|хранилище)\s*[:\-]?\s*"
        r"(\d+\s*(?:гб|gb|тб|tb)\s*(?:ssd|hdd|nvme)?)"
        r"|(\d+\s*(?:гб|gb|тб|tb)\s*(?:ssd|hdd|nvme))"
        r"|(\d+\s*(?:тб|tb)\b)"
    )),
    ("Экран", re.compile(
        r"(?i)(?:диагональ|экран|display|screen)\s*[:\-]?\s*(\d+(?:[.,]\d+)?\s*(?:\"|″|дюйм\w*))"
        r"|(\d+(?:[.,]\d+)?\s*(?:\"|″)\s*(?:fhd|hd|tn|ips)?)"
        r"|(\d+(?:[.,]\d+)?\s*(?:\"|″))"
    )),
    ("Разрешение", re.compile(r"(?i)(\d{3,4}\s*[xх×]\s*\d{3,4})")),
    ("Масса", re.compile(
        r"(?i)(?:вес|масса|weight)\s*[:\-]?\s*(\d+(?:[.,]\d+)?\s*кг)"
        r"|(\d+(?:[.,]\d+)?\s*кг(?:\s*без\s*кабеля)?)"
    )),
    ("Мощность", re.compile(
        r"(?i)(?:мощность|power(?:\s*input)?|двигатель)\s*[:\-]?\s*(\d+(?:[.,]\d+)?\s*(?:вт|w|квт|kw))"
        r"|(\d+(?:[.,]\d+)?\s*(?:вт|w)\b)"
    )),
    ("Скорость", re.compile(
        r"(?i)(?:скорость|частота(?:\s*вращения)?|no-?load\s*speed|макс\.?\s*число\s*оборот\w*)\s*[:\-]?\s*"
        r"(\d[\d\s,]*(?:\s*[-–—]\s*\d[\d\s,]*)?\s*(?:об/?мин|rpm))"
        r"|(\d[\d\s,]+\s*[-–—]\s*\d[\d\s,]+\s*(?:об/?мин|rpm))"
        r"|((?:0\s*[-–—]\s*)?\d{3,5}\s*(?:об/?мин|rpm))"
    )),
    ("Крутящий момент", re.compile(
        r"(?i)(?:крутящ\w*\s*момент|torque|макс(?:имальн\w*)?\s*крутящ\w*\s*момент)\s*[:\-]?\s*"
        r"(\d+(?:[.,]\d+)?\s*(?:н\s*[·•.]?\s*м|n\s*[·•.]?\s*m|nm))"
        r"|(\d+(?:[.,]\d+)?\s*(?:н\s*[·•.]?\s*м|n\s*[·•.]?\s*m|nm)\b)"
    )),
]

# Which structured labels make sense for a product kind (empty = all).
KIND_SPEC_LABELS: dict[str, frozenset[str]] = {
    "ноутбук": frozenset({"Процессор", "ОЗУ", "Накопитель", "Экран", "Разрешение", "Масса"}),
    "смартфон": frozenset({"ОЗУ", "Накопитель", "Экран", "Разрешение", "Масса"}),
    "дрель": frozenset({"Масса", "Мощность", "Скорость", "Крутящий момент"}),
    "шуруповёрт": frozenset({"Масса", "Мощность", "Скорость", "Крутящий момент"}),
}

# Series names belong in the model, not the brand.
SERIES_NAMES = frozenset({
    "ideapad", "thinkpad", "legion", "yoga", "galaxy", "redmi", "poco",
    "macbook", "iphone", "ipad", "pixel", "matebook", "surface",
})

BRAND_STOPWORDS = frozenset({
    "the", "and", "for", "with", "from", "pro", "professional", "original",
    "manual", "instructions", "pdf", "http", "https", "www", "com", "ru",
    "html", "view", "download", "impact", "drill", "power", "tool", "tools",
    "product", "products", "series", "model", "type", "size", "new", "set",
    "notebook", "laptop", "ultrabook", "tablet", "phone", "smartphone",
    *SERIES_NAMES,
})

# Charger wattage is not a useful laptop/phone product fact.
SKIP_POWER_KINDS = frozenset({"ноутбук", "смартфон", "монитор", "принтер"})

# ddgs ships Yandex as disabled=True; without it "yandex" falls back to auto and hangs.
SEARCH_BACKENDS = ("yandex", "duckduckgo")
SEARCH_TIMEOUT = 3
SEARCH_QUERY_TIMEOUT = 5
ENRICH_TOTAL_TIMEOUT = 7
MAX_SEARCH_QUERIES = 2


def detect_product_kind(text: str) -> str:
    for pattern, kind in PRODUCT_KIND_PATTERNS:
        if pattern.search(text):
            return kind
    return ""


def _numeric_model_after_brand(text: str, brand: str) -> str:
    if not brand:
        return ""
    match = re.search(
        rf"(?i)(?<![A-Za-z0-9]){re.escape(brand)}\s+(\d{{3,5}}[A-Za-z]{{0,3}})(?![A-Za-z0-9./])",
        text,
    )
    if not match:
        return ""
    candidate = match.group(1)
    if YEAR_MODEL_RE.fullmatch(candidate):
        return ""
    return candidate


def detect_product_identity(text: str) -> dict[str, str] | None:
    """Return brand/model if the description mentions a recognizable product identity."""
    brand = ""
    model = ""
    if match := EXPLICIT_BRAND_RE.search(text):
        brand = match.group(1).strip(" .,;:\"'«»")
    if match := EXPLICIT_MODEL_RE.search(text):
        model = match.group(1).strip(" .,;:\"'«»")

    model_match = MODEL_RE.search(text)
    if model_match and not model:
        model = re.sub(r"\s+", " ", model_match.group(1)).strip()
        before = text[: model_match.start()]
        # Keep series tokens (Galaxy, IdeaPad) for model assembly; drop other stopwords.
        allowed_stop = BRAND_STOPWORDS - SERIES_NAMES
        tokens = [
            token for token in LATIN_TOKEN_RE.findall(before)
            if token.casefold() not in allowed_stop
            and token.casefold() not in model.casefold()
        ]
        if tokens and not brand:
            if tokens[-1].casefold() in SERIES_NAMES:
                series = tokens[-1]
                brand = tokens[-2] if len(tokens) >= 2 else ""
                if series.casefold() not in model.casefold():
                    model = f"{series} {model}"
            else:
                brand = tokens[-1]

    if not brand:
        tokens = [
            token for token in LATIN_TOKEN_RE.findall(text)
            if token.casefold() not in BRAND_STOPWORDS
            and (not model or token.casefold() not in model.casefold())
        ]
        if tokens:
            brand = tokens[0]

    # Makita 6827 / Bosch 7805 — digit SKUs are not matched by MODEL_RE.
    if brand and not model:
        model = _numeric_model_after_brand(text, brand)
    if not model:
        for match in NUMERIC_MODEL_RE.finditer(text):
            candidate = match.group(1)
            if YEAR_MODEL_RE.fullmatch(candidate):
                continue
            # Only accept bare numeric SKU when a brand is already known.
            if brand:
                model = candidate
                break

    brand = brand.strip()
    model = model.strip()
    if brand and brand.casefold() in BRAND_STOPWORDS:
        brand = ""
    if brand and brand.islower():
        brand = brand.title()
    if model:
        # Normalize casing: IdeaPad 320, Galaxy A52, GSB 550, 6827
        parts = model.split()
        fixed: list[str] = []
        for part in parts:
            if part.casefold() in SERIES_NAMES:
                fixed.append(part[0].upper() + part[1:] if part.islower() else part)
            elif part.islower() and any(ch.isdigit() for ch in part):
                fixed.append(part.upper() if len(part) <= 6 else part)
            else:
                fixed.append(part)
        model = " ".join(fixed)
    if not brand and not model:
        return None
    kind = detect_product_kind(text)
    return {"brand": brand, "model": model, "kind": kind}


def _ensure_yandex_enabled() -> None:
    from ddgs import DDGS
    from ddgs.engines import ENGINES
    from ddgs.engines.yandex import Yandex

    Yandex.disabled = False
    ENGINES.setdefault("text", {})["yandex"] = Yandex
    DDGS.threads = 1


def _normalize_hits(rows: list[dict[str, Any]], *, limit: int) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in rows:
        title = str(row.get("title") or "").strip()
        href = str(row.get("href") or row.get("url") or "").strip()
        body = str(row.get("body") or "").strip()
        key = href or f"{title}|{body}"
        if not key or key in seen:
            continue
        seen.add(key)
        result.append({"title": title, "url": href, "snippet": body})
        if len(result) >= limit:
            break
    return result


def _rank_sources(sources: list[dict[str, str]], identity: dict[str, str]) -> list[dict[str, str]]:
    brand = identity.get("brand", "").casefold()
    model = identity.get("model", "").casefold()
    model_tokens = [t for t in re.findall(r"[a-zа-яё0-9]+", model, re.I) if len(t) >= 2]

    def score(item: dict[str, str]) -> tuple[int, int]:
        blob = f"{item.get('title', '')} {item.get('snippet', '')}".casefold()
        points = 0
        if brand and brand in blob:
            points += 3
        if model and model in blob:
            points += 5
        points += sum(2 for token in model_tokens if token in blob)
        if identity.get("kind") and identity["kind"] in blob:
            points += 2
        if NOISE_RE.search(blob):
            points -= 2
        if ACCESSORY_RE.search(blob):
            points -= 8
        if MEASURE_RE.search(blob) or any(p.search(blob) for _, p in SPEC_EXTRACTORS):
            points += 3
        if PRICE_RE.search(blob):
            points -= 1
        # Prefer product pages over spare-part listings for tools.
        if identity.get("kind") in {"дрель", "шуруповёрт"} and re.search(
            r"(?i)\b(?:мощность|вес|масса|об/?мин|н\s*[·•.]?\s*м|вт)\b", blob,
        ):
            points += 4
        return (-points, len(item.get("snippet") or ""))

    return sorted(sources, key=score)


def _search_ddgs(query: str, max_results: int = 5) -> list[dict[str, str]]:
    from ddgs import DDGS
    from ddgs.exceptions import DDGSException

    _ensure_yandex_enabled()
    for backend in SEARCH_BACKENDS:
        try:
            with DDGS(timeout=SEARCH_TIMEOUT) as client:
                rows = list(client.text(
                    query, region="ru-ru", max_results=max_results, backend=backend,
                ))
            hits = _normalize_hits(rows, limit=max_results)
            if hits:
                logger.info("web search ok via backend=%s query=%r n=%s", backend, query, len(hits))
                return hits
            logger.info("web search backend=%s empty for %r", backend, query)
        except DDGSException as exc:
            logger.warning("web search backend=%s failed: %s", backend, exc)
        except Exception as exc:
            logger.warning("web search backend=%s failed: %s", backend, exc)
    return []


def _normalize_fact(value: str) -> str:
    value = re.sub(r"\s+", " ", value).strip(" .,;:-•·|")
    value = re.sub(r"\s*([:;])\s*", r"\1 ", value)
    value = re.sub(r"(?i)\bgb\b", "ГБ", value)
    value = re.sub(r"(?i)\btb\b", "ТБ", value)
    value = re.sub(r"(?i)\bw\b", "Вт", value)
    return value.strip()


_VALID_RAM_GB = frozenset({1, 2, 3, 4, 6, 8, 12, 16, 18, 24, 32, 36, 48, 64, 96, 128})


def _canonicalize_value(label: str, value: str) -> str:
    value = _normalize_fact(value)
    if label == "Процессор":
        value = re.sub(r"(?i)^i([3579])", r"Intel Core i\1", value)
        value = re.sub(r"(?i)^core\s+", "Intel Core ", value)
        value = re.sub(r"(?i)\s+", " ", value)
    if label == "ОЗУ":
        mb = re.fullmatch(r"(\d{4,5})", value.replace(" ", ""))
        if mb and int(mb.group(1)) in {2048, 4096, 8192, 16384, 32768}:
            value = f"{int(mb.group(1)) // 1024} ГБ"
        else:
            match = re.fullmatch(r"(?i)(\d+)\s*(гб|gb)(?:\s*(ddr\d*))?", value)
            if match and int(match.group(1)) in _VALID_RAM_GB:
                suffix = f" {match.group(3).upper()}" if match.group(3) else ""
                value = f"{int(match.group(1))} ГБ{suffix}"
            elif not re.search(r"(?i)гб|gb", value):
                value = ""  # drop unitless garbage like bare "28"
    if label == "Накопитель":
        match = re.fullmatch(r"(?i)(\d+)\s*(гб|gb|тб|tb)\s*(ssd|hdd|nvme)?", value)
        if match:
            unit = {"gb": "ГБ", "гб": "ГБ", "tb": "ТБ", "тб": "ТБ"}[match.group(2).casefold()]
            kind = f" {match.group(3).upper()}" if match.group(3) else ""
            value = f"{int(match.group(1))} {unit}{kind}"
    if label == "Крутящий момент":
        match = re.search(r"(?i)(\d+(?:[.,]\d+)?)\s*(?:н\s*[·•.]?\s*м|n\s*[·•.]?\s*m|nm)", value)
        if match:
            value = f"{match.group(1).replace('.', ',')} Н·м"
    if label == "Скорость":
        value = value.replace(",", "")
        match = re.search(
            r"(?i)((?:0\s*[-–—]\s*)?\d+(?:\s*[-–—]\s*\d+)?)\s*(?:об/?мин|rpm)", value,
        )
        if match:
            value = f"{match.group(1).replace('—', '-').replace('–', '-')} об/мин"
    return value


def _already_present(fact: str, text: str) -> bool:
    hay = text.casefold().replace("ё", "е")
    needle = fact.casefold().replace("ё", "е")
    if needle in hay:
        return True
    # Numeric/spec tokens already mentioned.
    for match in MEASURE_RE.finditer(fact):
        token = match.group().casefold().replace(" ", "")
        if len(token) >= 3 and token in hay.replace(" ", ""):
            return True
    cpu = re.search(r"(?i)(?:core\s+i[3579][-\w]*|ryzen\s*\d+\w*|a\d{1,2}\s*\d{3,})", fact)
    if cpu and cpu.group().casefold().replace(" ", "") in hay.replace(" ", ""):
        return True
    return False


def _is_weak_fact(fact: str) -> bool:
    lowered = fact.casefold()
    if len(fact) < 6 or len(fact) > 120:
        return True
    if NOISE_RE.search(fact) or MARKETING_RE.search(fact) or PRICE_RE.search(fact):
        return True
    if not re.search(r"\d", fact):
        return True
    if LISTING_TITLE_RE.search(fact) and ":" not in fact:
        return True
    # Bare labels / useless fragments.
    if re.fullmatch(r"(?i)[\w\s]{1,20}", fact) and not MEASURE_RE.search(fact):
        return True
    if "все характеристики" in lowered or "о товаре" in lowered:
        return True
    # Long SKU / article codes.
    if re.search(r"\b\d{8,}\b", fact) and ":" not in fact:
        return True
    return False


def _format_spec(label: str, value: str) -> str:
    value = _canonicalize_value(label, value)
    if not value:
        return ""
    if label.casefold() in value.casefold():
        return value
    return f"{label}: {value}"


def _fact_key(fact: str) -> str:
    return re.sub(r"\s+", " ", fact).casefold().replace("ё", "е")


def _collect_phone_combo_facts(blob: str, text: str) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for match in PHONE_COMBO_RE.finditer(blob):
        ram, storage = match.group(1), match.group(2)
        ram_fact = _format_spec("ОЗУ", f"{ram} ГБ")
        storage_fact = _format_spec("Накопитель", f"{storage} ГБ")
        if not _is_weak_fact(ram_fact) and not _already_present(ram_fact, text):
            found.append(("ОЗУ", ram_fact))
        if not _is_weak_fact(storage_fact) and not _already_present(storage_fact, text):
            found.append(("Накопитель", storage_fact))
    return found


def _collect_mb_ram_facts(blob: str, text: str) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for match in MB_RAM_RE.finditer(blob):
        gb = int(match.group(1)) // 1024
        fact = _format_spec("ОЗУ", f"{gb} ГБ")
        if not _is_weak_fact(fact) and not _already_present(fact, text):
            found.append(("ОЗУ", fact))
    return found


def extract_structured_facts(
    text: str,
    sources: list[dict[str, str]],
    *,
    limit: int = 6,
    kind: str = "",
) -> list[str]:
    allowed = KIND_SPEC_LABELS.get(kind)
    votes: dict[str, list[str]] = {label: [] for label, _ in SPEC_EXTRACTORS}
    for item in sources:
        blob = f"{item.get('title', '')}. {item.get('snippet', '')}"
        if not allowed or "ОЗУ" in allowed:
            for label, fact in _collect_phone_combo_facts(blob, text):
                votes.setdefault(label, []).append(fact)
            for label, fact in _collect_mb_ram_facts(blob, text):
                votes.setdefault(label, []).append(fact)
        for label, pattern in SPEC_EXTRACTORS:
            if allowed is not None and label not in allowed:
                continue
            if label == "Мощность" and kind in SKIP_POWER_KINDS:
                continue
            for match in pattern.finditer(blob):
                value = next((g for g in match.groups() if g), None)
                if not value:
                    continue
                fact = _format_spec(label, value)
                if not fact or _is_weak_fact(fact) or _already_present(fact, text):
                    continue
                # Bare N ГБ after phone combo already covered; skip tiny RAM false positives.
                if label == "ОЗУ" and re.fullmatch(r"(?i)озу:\s*[123]\s*гб", fact) and kind == "ноутбук":
                    continue
                votes.setdefault(label, []).append(fact)

    facts: list[str] = []
    seen_labels: set[str] = set()
    for label, _ in SPEC_EXTRACTORS:
        if label in seen_labels:
            continue
        if allowed is not None and label not in allowed:
            continue
        candidates = votes.get(label) or []
        if not candidates:
            continue
        best, count = Counter(_fact_key(c) for c in candidates).most_common(1)[0]
        # Prefer the first original casing that matches the winning key.
        winner = next(c for c in candidates if _fact_key(c) == best)
        # Require at least some agreement when sources disagree heavily.
        if len(candidates) >= 3 and count == 1 and label == "Процессор":
            # Still take the top-ranked source's first hit (candidates preserve source order).
            winner = candidates[0]
        facts.append(winner)
        seen_labels.add(label)
        if len(facts) >= limit:
            break
    return facts


def extract_facts(
    text: str,
    sources: list[dict[str, str]],
    characteristic_names: list[str] | None = None,
    *,
    limit: int = 6,
    kind: str = "",
) -> list[str]:
    structured = extract_structured_facts(text, sources, limit=limit, kind=kind)
    if len(structured) >= 2:
        return structured[:limit]
    # Tool pages are noisy (bits, brushes, packaging) — never fall back to free clauses.
    if kind in {"дрель", "шуруповёрт"}:
        return structured[:limit]

    names = [
        re.sub(r"\s+", " ", name).strip().casefold().replace("ё", "е")
        for name in characteristic_names or []
        if name.strip()
    ]
    name_roots = [name[: max(4, len(name) // 2)] for name in names if len(name) >= 4]
    scored: list[tuple[int, str]] = []
    seen: set[str] = set()
    for source in sources:
        blob = f"{source.get('title', '')}. {source.get('snippet', '')}"
        for raw in CLAUSE_SPLIT_RE.split(blob):
            clause = _normalize_fact(raw)
            if _is_weak_fact(clause):
                continue
            lowered = clause.casefold().replace("ё", "е")
            measures = MEASURE_RE.findall(clause)
            has_measure = bool(measures)
            has_name = any(root in lowered for root in name_roots)
            if not has_measure and not has_name:
                continue
            if _already_present(clause, text):
                continue
            key = lowered
            if key in seen:
                continue
            seen.add(key)
            score = len(measures) * 4 + (1 if has_name else 0) - (1 if len(clause) > 90 else 0)
            scored.append((score, clause))
    scored.sort(key=lambda item: (-item[0], len(item[1])))
    merged = list(dict.fromkeys([*structured, *[clause for _, clause in scored]]))
    return merged[:limit]


def build_enrichment(
    text: str,
    identity: dict[str, str],
    sources: list[dict[str, str]],
    facts: list[str],
) -> dict[str, Any] | None:
    facts = [fact for fact in facts if not _is_weak_fact(fact)]
    if not facts:
        return None
    brand = identity.get("brand", "")
    model = identity.get("model", "")
    label = " ".join(part for part in (brand, model) if part).strip()
    base = text.rstrip().rstrip(".")
    suggested = f"{base}. {'; '.join(facts)}."
    return {
        "brand": brand,
        "model": model,
        "query": " ".join(part for part in (identity.get("kind", ""), brand, model, "характеристики") if part).strip(),
        "sources": sources[:5],
        "facts": facts,
        "suggestedDescription": suggested,
        "message": (
            f"В описании найдены бренд/модель «{label}». "
            "По открытым источникам можно добавить характеристики товара."
        ),
    }


def _search_queries(identity: dict[str, str], text: str = "") -> list[str]:
    brand = identity.get("brand", "").strip()
    model = identity.get("model", "").strip()
    if not model:
        return []
    kind = identity.get("kind") or detect_product_kind(text)
    plain = " ".join(part for part in (brand, model) if part).strip()
    # Quoting pure digit SKUs (Makita "6827") often returns zero hits.
    numeric_sku = bool(re.fullmatch(r"\d{3,5}[A-Za-z]{0,3}", model))
    kind_prefix = f"{kind} " if kind else ""
    queries: list[str] = []
    if kind in {"дрель", "шуруповёрт"} or numeric_sku:
        queries.append(f"{plain} характеристики мощность вес")
    queries.append(f"{plain} характеристики")
    if kind_prefix and not numeric_sku:
        quoted = f'{brand} "{model}"'.strip() if brand else f'"{model}"'
        queries.insert(0, f"{kind_prefix}{quoted} характеристики".strip())
        queries.append(f"{kind_prefix}{plain} характеристики".strip())
    elif kind_prefix:
        queries.append(f"{kind_prefix}{plain} характеристики".strip())
    if kind in {"ноутбук", "смартфон"}:
        queries.append(f"{plain} спецификация процессор память")
    return list(dict.fromkeys(q for q in queries if len(q) >= 4))[:MAX_SEARCH_QUERIES]


def resolve_identity(
    text: str,
    *,
    brand: str = "",
    model: str = "",
) -> dict[str, str] | None:
    """Merge autofill product fields with regex fallback (Makita 6827, IdeaPad 320…)."""
    brand = brand.strip()
    model = model.strip()
    detected = detect_product_identity(text)
    if detected:
        brand = brand or detected.get("brand", "")
        model = model or detected.get("model", "")
        kind = detected.get("kind", "") or detect_product_kind(text)
    else:
        kind = detect_product_kind(text)
    if brand and brand.islower():
        brand = brand.title()
    if not brand and not model:
        return None
    return {"brand": brand, "model": model, "kind": kind}


def identity_from_product(product: dict[str, Any] | None, text: str) -> dict[str, str] | None:
    brand = ""
    model = ""
    if isinstance(product, dict):
        manufacturer = product.get("manufacturer") or {}
        model_row = product.get("model") or {}
        if manufacturer.get("status") == "accepted" and manufacturer.get("value"):
            brand = str(manufacturer["value"]).strip()
        if model_row.get("status") == "accepted" and model_row.get("value"):
            model = str(model_row["value"]).strip()
    return resolve_identity(text, brand=brand, model=model)


async def _enrich_from_web_uncapped(
    text: str,
    characteristic_names: list[str] | None = None,
    *,
    identity: dict[str, str] | None = None,
) -> dict[str, Any] | None:
    identity = identity or detect_product_identity(text)
    # Brand alone (e.g. "Шуруповерт Makita") is too ambiguous — skip the web round-trip.
    if identity is None or not identity.get("model"):
        return None
    if not identity.get("kind"):
        identity = {**identity, "kind": detect_product_kind(text)}
    best: dict[str, Any] | None = None
    best_score = 0
    for query in _search_queries(identity, text):
        try:
            sources = await asyncio.wait_for(
                asyncio.to_thread(_search_ddgs, query),
                timeout=SEARCH_QUERY_TIMEOUT,
            )
        except TimeoutError:
            logger.warning("web enrichment search timed out for %r", query)
            continue
        except Exception:
            logger.exception("web enrichment search failed for %r", query)
            continue
        if not sources:
            continue
        sources = _rank_sources(sources, identity)
        facts = extract_facts(
            text, sources, characteristic_names, kind=identity.get("kind", ""),
        )
        enrichment = build_enrichment(text, identity, sources, facts)
        if enrichment is None:
            continue
        labeled = sum(1 for fact in enrichment["facts"] if ":" in fact)
        if labeled == 0:
            continue
        score = len(enrichment["facts"]) * 2 + labeled
        if score > best_score:
            best = enrichment
            best_score = score
        if score >= 4:
            break
    return best


async def enrich_from_web(
    text: str,
    characteristic_names: list[str] | None = None,
    *,
    identity: dict[str, str] | None = None,
) -> dict[str, Any] | None:
    try:
        return await asyncio.wait_for(
            _enrich_from_web_uncapped(text, characteristic_names, identity=identity),
            timeout=ENRICH_TOTAL_TIMEOUT,
        )
    except TimeoutError:
        logger.warning("web enrichment exceeded %ss budget", ENRICH_TOTAL_TIMEOUT)
        return None
