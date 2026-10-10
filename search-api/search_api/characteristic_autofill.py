from __future__ import annotations

import asyncio
import math
import re
import time
from dataclasses import dataclass
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from search_api.config import ROOT, settings

NONE = "не указано"
MAX_CHOICES = 254
BATCH_SIZE = 4
NUMBER = r"-?(?:[1-9]\d{0,2}(?:\.\d{3})+(?![\d.,])|\d+(?:[.,]\d+)?)"
# Dimension components share the trailing unit, but keep their individual positions.
NUMBER_RE = re.compile(
    rf"(?<![\w.,-])(?P<numbers>{NUMBER}(?:[ \t]*[xх×*][ \t]*{NUMBER})*)"
    r"(?:[ \t]*(?P<unit>[a-zа-яё°%][\w/%.²³·-]*))?", re.I,
)
UNIT_GROUPS = [
    ("length", 0.001, ["мкм", "микрометр"]),
    ("length", 1, ["мм", "миллиметр"]),
    ("length", 10, ["см", "сантиметр"]),
    ("length", 100, ["дм", "дециметр"]),
    ("length", 1000, ["м", "метр"]),
    ("length", 1_000_000, ["км", "километр"]),
    ("mass", 0.001, ["мг", "миллиграмм"]),
    ("mass", 1, ["г", "гр", "грамм"]),
    ("mass", 1000, ["кг", "килограмм", "kg"]),
    ("mass", 1_000_000, ["т", "тонна"]),
    ("volume", 1, ["мл", "миллилитр", "см3", "кубический сантиметр"]),
    ("volume", 1000, ["л", "литр", "дм3", "кубический дециметр"]),
    ("volume", 1_000_000, ["м3", "кубический метр"]),
    ("area", 1, ["мм2", "квадратный миллиметр"]),
    ("area", 100, ["см2", "квадратный сантиметр"]),
    ("area", 1_000_000, ["м2", "квадратный метр"]),
    ("density", 1, ["г/м2", "г/кв.м", "грамм на квадратный метр", "gsm"]),
    ("power", 1, ["вт", "ватт", "w"]),
    ("power", 1000, ["квт", "киловатт", "kw"]),
    ("time", 1, ["с", "сек", "секунда"]),
    ("time", 60, ["мин", "минута"]),
    ("time", 3600, ["ч", "час"]),
    ("count", 1, ["шт", "штука", "ед", "единица"]),
    ("percent", 1, ["%", "процент"]),
]


class AutofillConfigError(RuntimeError):
    pass


class AutofillError(RuntimeError):
    pass


class CharacteristicRange(BaseModel):
    option: str = Field(min_length=1, max_length=2000)
    min: float | None = None
    max: float | None = None
    minInclusive: bool = True
    maxInclusive: bool = True
    model_config = ConfigDict(allow_inf_nan=False)

    def contains(self, value: float) -> bool:
        return (
            (self.min is None or (value >= self.min if self.minInclusive else value > self.min))
            and (self.max is None or (value <= self.max if self.maxInclusive else value < self.max))
        )


class AutofillCharacteristic(BaseModel):
    id: str = Field(min_length=1, max_length=300)
    name: str = Field(min_length=1, max_length=500)
    options: list[str] = Field(default_factory=list, max_length=MAX_CHOICES)
    optionLabels: dict[str, str] = Field(default_factory=dict, max_length=MAX_CHOICES)
    ranges: list[CharacteristicRange] = Field(default_factory=list, max_length=MAX_CHOICES)
    unit: str = Field(default="", max_length=200)
    numeric: bool = False
    multiple: bool = False
    hint: str = Field(default="", max_length=2000)

    @model_validator(mode="after")
    def valid_options(self):
        if any(not option.strip() or len(option) > 2000 or option == NONE for option in self.options):
            raise ValueError("Недопустимый вариант характеристики")
        if len(set(self.options)) != len(self.options):
            raise ValueError("Варианты характеристики должны быть уникальны")
        if any(item.option not in self.options for item in self.ranges):
            raise ValueError("Диапазон должен соответствовать варианту характеристики")
        return self


class AutofillRequest(BaseModel):
    text: str = Field(min_length=1, max_length=20000)
    characteristics: list[AutofillCharacteristic] = Field(min_length=1, max_length=100)
    standards: list[str] = Field(default_factory=list, max_length=50)

    @field_validator("text")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Описание не должно быть пустым")
        return value.strip()

    @model_validator(mode="after")
    def unique_ids(self):
        ids = [item.id for item in self.characteristics]
        if len(ids) != len(set(ids)):
            raise ValueError("Идентификаторы характеристик должны быть уникальны")
        return self


@dataclass(frozen=True)
class Candidate:
    label: str
    value: str
    unit: str
    evidence: str
    attribute: str = ""
    subjects: frozenset[str] = frozenset()
    dimension_group: bool = False


# These are labels, not assumed dimension orders. Unlabelled tuples stay ambiguous.
AXIS_NAMES = {"д": "длина", "ш": "ширина", "в": "высота", "г": "глубина"}
AXIS_RE = re.compile(r"длин[аыуе]|ширин[аыуе]|высот[аыуе]|глубин[аыуе]", re.I)
ORDER_RE = re.compile(
    r"(?<!\w)([дшвг])\s*[xх×*]\s*([дшвг])(?:\s*[xх×*]\s*([дшвг]))?(?!\w)", re.I,
)
SUBJECT_PATTERNS = {
    "ящик": r"ящик\w*",
    "полка": r"пол(?:ок|к\w*)",
    "открытый": r"открыт\w*",
    "ножка": r"нож(?:ек|к\w*)",
    "опора": r"опор\w*",
    "колесо": r"кол[её]с\w*",
    "упаковка": r"упаков\w*",
    "дверь": r"двер\w*",
    "корпус": r"корпус\w*",
    "рукоятка": r"рукоят\w*",
    "головка": r"голов\w*",
    "деталь": r"детал\w*",
}


def subject_terms(text: str) -> frozenset[str]:
    return frozenset(name for name, pattern in SUBJECT_PATTERNS.items()
                     if re.search(rf"(?<!\w){pattern}(?!\w)", text, re.I))


def noun_key(word: str) -> str:
    word = word.casefold().replace("ё", "е")
    if re.fullmatch(r"пол(?:ок|к\w*)", word):
        return "полк"
    if re.fullmatch(r"зам(?:ок|к\w*)", word):
        return "замк"
    return re.sub(r"(?:иями|ями|ами|ого|ему|ому|ией|ий|ой|ей|ов|ев|ам|ям|ах|ях|ия|ие|ии|ию|ы|и|а|я|у|ю|е|о|ь)$", "", word)


def parse_number(raw: str) -> float:
    if re.fullmatch(r"-?[1-9]\d{0,2}(?:\.\d{3})+", raw):
        return float(raw.replace(".", ""))
    return float(raw.replace(",", "."))


def parse_unit(raw: str) -> tuple[str, float] | None:
    text = raw.lower().replace("ё", "е").replace("²", "2").replace("³", "3").strip().rstrip(".")
    for part in text.split(";"):
        part = part.strip()
        for dimension, factor, aliases in UNIT_GROUPS:
            if part in aliases:
                return dimension, factor
        for dimension, factor, aliases in UNIT_GROUPS:
            if any(len(alias) >= 4 and part.startswith(alias.rstrip("аяь")) for alias in aliases):
                return dimension, factor
    return None


def number_candidates(text: str) -> list[Candidate]:
    result = []
    for match in NUMBER_RE.finditer(text):
        unit = match.group("unit") or ""
        # A word following the number is not automatically a measurement unit.
        if parse_unit(unit) is None:
            unit = ""
        # Keep the local clause separate from the broader evidence sent to the model.
        separators = list(re.finditer(r"[;\n]|(?<!\d),(?!\d)", text))
        start = max((item.end() for item in separators if item.end() <= match.start()), default=0)
        end = min((item.start() for item in separators if item.start() >= match.end()), default=len(text))
        clause = text[start:end]
        before = text[start:match.start()]
        after = text[match.end():end]
        components = list(re.finditer(NUMBER, match.group("numbers")))
        grouped = len(components) > 1
        order = ORDER_RE.search(clause) if grouped else None
        axes = [AXIS_NAMES[axis.lower()] for axis in order.groups() if axis] if order else []
        axis_labels = list(AXIS_RE.finditer(before))
        axis = AXIS_NAMES[axis_labels[-1].group()[0].lower()] if axis_labels else ""
        # Count nouns immediately next to a scalar identify what is being counted.
        trailing = match.group("unit") or ""
        suffix = f"{trailing} {after}" if trailing and parse_unit(trailing) is None else after
        words = re.findall(r"[а-яё]+", suffix, re.I)
        # Allow explicit qualifiers (e.g. «5 открытых полок»), not unrelated clauses.
        while len(words) > 1 and re.search(r"(?:ых|их|ые|ие|ая|яя|ое|ее)$", words[0], re.I):
            words.pop(0)
        count_noun = words[0] if words else ""
        if re.search(r"количество|число|кол-во", before, re.I):
            words = re.findall(r"[а-яё]+", before, re.I)
            count_noun = words[-1] if words else count_noun
        evidence = text[max(0, match.start() - 60):match.end() + 60]
        subjects = subject_terms(before if grouped else clause)
        for component, number in enumerate(components, 1):
            attribute = axes[component - 1] if len(axes) == len(components) else ""
            if not grouped:
                counted = count_noun and (not unit or parse_unit(unit)[0] == "count")
                attribute = axis or (f"count:{noun_key(count_noun)}" if counted else "")
            value = format(parse_number(number.group()), ".12g")
            label = f"{value} {unit} (вхождение {len(result) + 1}, компонент {component})".strip()
            result.append(Candidate(label, value, unit, evidence, attribute, subjects, grouped))
            if len(result) == MAX_CHOICES:
                return result
    return result


def text_candidates(text: str) -> list[Candidate]:
    # System One selects only source spans, never generates unsupported free text.
    spans = re.split(r"[;\n]|(?<!\d)[,.](?!\d)", text)
    words = re.findall(r"[\w]+(?:[-/][\w]+)*", text)
    values = dict.fromkeys([part.strip() for part in spans] + words)
    return [Candidate(value, value, "", value) for value in values if value][:MAX_CHOICES]


def numeric_candidates(field: AutofillCharacteristic, candidates: list[Candidate]) -> list[Candidate]:
    target = parse_unit(field.unit)
    axis_match = AXIS_RE.search(field.name)
    axis = AXIS_NAMES[axis_match.group()[0].lower()] if axis_match else ""
    count_match = re.search(r"(?:количество|число|кол-во)\s+(.+)", field.name, re.I)
    count_words = re.findall(r"[а-яё]+", count_match.group(1), re.I) if count_match else []
    count_noun = noun_key(count_words[-1]) if count_words else ""
    subjects = subject_terms(field.name)
    result = []
    for candidate in candidates:
        if candidate.dimension_group and not axis:
            continue
        if axis and candidate.attribute != axis:
            continue
        if (axis or count_noun) and candidate.subjects != subjects:
            continue
        if candidate.attribute.startswith("count:") and not count_noun:
            continue
        if count_noun:
            if candidate.attribute != f"count:{count_noun}" or candidate.subjects != subjects:
                continue
        source = parse_unit(candidate.unit)
        if source and target and source[0] != target[0]:
            continue
        # Never relabel an unknown physical unit as a known one.
        if field.unit and candidate.unit and target is None and candidate.unit.lower() != field.unit.lower():
            continue
        value = float(candidate.value)
        if source and target:
            value *= source[1] / target[1]
        result.append(Candidate(candidate.label, format(value, ".12g"), field.unit or candidate.unit, candidate.evidence))
    return result


def build_questions(body: AutofillRequest) -> tuple[dict[str, Any], dict[str, tuple[AutofillCharacteristic, dict[str, Candidate]]]]:
    numbers = number_candidates(body.text)
    texts = text_candidates(body.text)
    questions = {}
    bindings = {}
    for index, field in enumerate(body.characteristics):
        numeric = field.numeric or bool(field.ranges) or (bool(field.unit) and not field.options)
        if numeric:
            candidates = numeric_candidates(field, numbers)
        elif field.options:
            candidates = [Candidate(option, option, "", field.optionLabels.get(option, option)) for option in field.options]
        else:
            candidates = texts
        if not candidates:
            continue
        context = {
            "характеристика": field.name, "единица": field.unit,
            "подсказка схемы": field.hint, "стандарты": body.standards,
            "question": "Какое значение характеристики подтверждается описанием товара?",
            "правило": (
                "Используй только описание товара, не придумывай отсутствующие свойства. "
                "При отсутствии или неоднозначности выбирай «не указано». "
                "Для чисел учитывай подписи и единицы. Не угадывай порядок неподписанных размеров, "
                "не подменяй размер товара размером упаковки или детали. "
                "Варианты-вхождения содержат точную цитату и номер компонента размеров. "
                "Полностью учитывай уточнения в имени характеристики: полки не означают открытые полки, "
                "полки не означают отделения; материал не означает фактуру поверхности. "
                "Слово «металлический» не определяет сплав или марку стали. "
                "Ограничение схемы не является значением из описания. "
                "Не выводи расшифровку числовых кодов ГОСТ, если ее нет в схеме или описании."
            ),
        }
        if field.multiple and field.options and not numeric:
            for option_index, candidate in enumerate(candidates):
                qid = f"c{index}_{option_index}"
                questions[qid] = {
                    "type": "choice", "instructions": context,
                    "criteria": {candidate.label: candidate.evidence, NONE: "Этот вариант не подтвержден описанием"},
                }
                bindings[qid] = field, {candidate.label: candidate}
        else:
            qid = f"c{index}"
            questions[qid] = {
                "type": "choice", "instructions": context,
                "criteria": {candidate.label: candidate.evidence for candidate in candidates}
                | {NONE: "В описании нет однозначного значения этой характеристики"},
            }
            bindings[qid] = field, {candidate.label: candidate for candidate in candidates}
    if len(questions) > 400:
        raise ValueError("Слишком много вариантов для автозаполнения")
    return questions, bindings


def typesafe_key() -> str:
    if settings.typesafe_api_key is not None:
        key = settings.typesafe_api_key.get_secret_value().strip()
    else:
        path = ROOT / "secrets" / "typesafe-token"
        key = path.read_text(encoding="utf-8").strip() if path.is_file() else ""
    if not key:
        raise AutofillConfigError("Не настроен TYPESAFE_API_KEY или secrets/typesafe-token")
    return key


async def autofill_characteristics(body: AutofillRequest) -> dict[str, Any]:
    started = time.perf_counter()
    key = typesafe_key()
    questions, bindings = build_questions(body)
    answers = {}
    semaphore = asyncio.Semaphore(4)
    base_url = settings.typesafe_base_url.rstrip("/")
    usage = {"input_tokens": 0, "output_tokens": 0}
    model = settings.typesafe_model
    async with httpx.AsyncClient(timeout=settings.typesafe_timeout) as client:
        async def ask(batch: dict[str, Any]):
            async with semaphore:
                try:
                    response = await client.post(
                        f"{base_url}/v1/systemone",
                        headers={"Authorization": f"Bearer {key}"},
                        json={"model": settings.typesafe_model, "state": {"товар": body.text}, "questions": batch},
                    )
                    response.raise_for_status()
                    payload = response.json()
                    if not isinstance(payload, dict) or not isinstance(payload.get("answers"), dict):
                        raise AutofillError("TypeSafe вернул некорректный ответ")
                    if set(payload["answers"]) != set(batch):
                        raise AutofillError("TypeSafe вернул неполный набор ответов")
                    for qid, answer in payload["answers"].items():
                        if not isinstance(answer, dict) or answer.get("choice") not in batch[qid]["criteria"]:
                            raise AutofillError("TypeSafe вернул недопустимый вариант")
                        probabilities = answer.get("probabilities")
                        if not isinstance(probabilities, dict) or set(probabilities) != set(batch[qid]["criteria"]):
                            raise AutofillError("TypeSafe вернул неполный набор вероятностей")
                        if any(isinstance(score, bool) or not isinstance(score, (int, float))
                               or not math.isfinite(score) or not 0 <= score <= 1 for score in probabilities.values()):
                            raise AutofillError("TypeSafe вернул некорректную вероятность")
                    return payload
                except httpx.TimeoutException as exc:
                    raise AutofillError("TypeSafe не ответил вовремя") from exc
                except httpx.HTTPStatusError as exc:
                    raise AutofillError(f"TypeSafe API: HTTP {exc.response.status_code}") from exc
                except (httpx.RequestError, ValueError) as exc:
                    raise AutofillError("Не удалось получить ответ TypeSafe") from exc

        items = list(questions.items())
        responses = await asyncio.gather(*(ask(dict(items[i:i + BATCH_SIZE])) for i in range(0, len(items), BATCH_SIZE)))
        for response in responses:
            answers.update(response["answers"])
            model = response.get("model", model)
            for name in usage:
                usage[name] += (response.get("usage") or {}).get(name) or 0

    filled: dict[str, dict[str, Any]] = {}
    option_probabilities: dict[str, dict[str, float]] = {}
    for qid, answer in answers.items():
        field, candidates = bindings[qid]
        if field.options:
            scores = option_probabilities.setdefault(field.id, dict.fromkeys(field.options, 0.0))
            for label, candidate in candidates.items():
                score = answer["probabilities"][label]
                if field.ranges:
                    option = next((item.option for item in field.ranges if item.contains(float(candidate.value))), None)
                    if option is not None:
                        scores[option] += score
                else:
                    scores[candidate.value] = score
        choice = answer["choice"]
        score = answer["probabilities"][choice]
        candidate = candidates.get(choice)
        if candidate is None or score < settings.typesafe_confidence:
            continue
        range_option = ""
        if field.ranges:
            range_option = next((item.option for item in field.ranges if item.contains(float(candidate.value))), "")
        value = candidate.value
        if field.numeric and not field.unit and not field.ranges and candidate.unit:
            value = f"{value} {candidate.unit}"
        previous = filled.get(field.id)
        if previous and field.multiple:
            previous["value"] += " · " + value
            previous["confidence"] = min(previous["confidence"], score)
        else:
            filled[field.id] = {
                "id": field.id, "value": value, "rangeOption": range_option,
                "confidence": score, "evidence": candidate.evidence,
            }
    return {
        "characteristics": list(filled.values()), "optionProbabilities": option_probabilities,
        "model": model, "usage": usage,
        "durationMs": round((time.perf_counter() - started) * 1000),
    }
