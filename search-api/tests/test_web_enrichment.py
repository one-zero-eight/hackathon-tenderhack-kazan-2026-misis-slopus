import asyncio

from search_api import web_enrichment as enrich


def test_detects_brand_and_model():
    identity = enrich.detect_product_identity("Ударная дрель Bosch GSB 550 Professional")
    assert identity == {"brand": "Bosch", "model": "GSB 550", "kind": "дрель"}
    assert enrich.detect_product_identity("Ноутбук lenovo a320 14 дюймов") == {
        "brand": "Lenovo", "model": "A320", "kind": "ноутбук",
    }
    assert enrich.detect_product_identity("Lenovo IdeaPad 320") == {
        "brand": "Lenovo", "model": "IdeaPad 320", "kind": "",
    }
    assert enrich.detect_product_identity("Смартфон Samsung Galaxy A52") == {
        "brand": "Samsung", "model": "Galaxy A52", "kind": "смартфон",
    }
    assert enrich.detect_product_identity("Шуруповерт makita 6827") == {
        "brand": "Makita", "model": "6827", "kind": "шуруповёрт",
    }


def test_detects_explicit_brand_or_model():
    assert enrich.detect_product_identity("бренд Makita, сетевой шуруповёрт")["brand"] == "Makita"
    assert enrich.detect_product_identity("Модель: XR2000, аккумуляторный")["model"] == "XR2000"


def test_skips_generic_description_without_identity():
    assert enrich.detect_product_identity("Молоток слесарный 500 г, рукоятка деревянная") is None


def test_extract_facts_skips_values_already_in_description():
    sources = [{
        "title": "Bosch GSB 550",
        "url": "https://example.com",
        "snippet": "Мощность 550 Вт; скорость 0-2700 об/мин; вес 1.8 кг",
    }]
    facts = enrich.extract_facts(
        "Дрель Bosch GSB 550, мощность 550 Вт",
        sources,
        ["Мощность", "Масса", "Скорость"],
    )
    assert any("2700" in fact for fact in facts)
    assert any("1.8 кг" in fact or "1.8" in fact for fact in facts)
    assert not any("550 Вт" in fact and "мощность" in fact.casefold() for fact in facts)


def test_extract_structured_laptop_facts():
    sources = [{
        "title": 'Ноутбук Lenovo IdeaPad 320-15IKB 15.6", Intel Core i3 7100U',
        "url": "https://example.com",
        "snippet": "ОЗУ: 4 Гб DDR4; Диск: 500 Гб HDD; Вес: 1.9 кг. Купите сейчас в корзину!",
    }]
    facts = enrich.extract_facts(
        "Lenovo IdeaPad 320", sources, ["Процессор", "ОЗУ", "Накопитель", "Масса"], kind="ноутбук",
    )
    joined = " | ".join(facts).casefold()
    assert "core i3" in joined or "7100u" in joined
    assert "4" in joined and ("гб" in joined or "gb" in joined)
    assert not any("корзин" in fact.casefold() for fact in facts)


def test_extract_phone_combo_and_skips_listing_junk():
    sources = [
        {
            "title": "Смартфон Samsung Galaxy A52 6/128 ГБ, лаванда 23 000 ₽",
            "url": "https://example.com/a",
            "snippet": "Купить смартфон Galaxy A52 по низкой цене с доставкой",
        },
        {
            "title": "Samsung Galaxy A52 характеристики",
            "url": "https://example.com/b",
            "snippet": "Экран 6.5\". Память 6/128ГБ. Процессор Snapdragon.",
        },
    ]
    facts = enrich.extract_facts(
        "Смартфон Samsung Galaxy A52", sources, ["ОЗУ", "Накопитель", "Экран"], kind="смартфон",
    )
    joined = " | ".join(facts).casefold()
    assert "озу: 6" in joined
    assert "128" in joined
    assert "озу: 28" not in joined
    assert not any("руб" in f.casefold() or "₽" in f or "лаванда" in f.casefold() for f in facts)


def test_consensus_prefers_repeated_cpu():
    sources = [
        {"title": "AMD A12", "url": "a", "snippet": "Lenovo IdeaPad 320 AMD A12 8GB"},
        {"title": "i5 7200U", "url": "b", "snippet": "IdeaPad 320 Intel Core i5-7200U"},
        {"title": "AMD A12 again", "url": "c", "snippet": "Ноутбук IdeaPad 320 на AMD A12"},
    ]
    facts = enrich.extract_structured_facts("Lenovo IdeaPad 320", sources, kind="ноутбук")
    joined = " | ".join(facts).casefold()
    assert "a12" in joined
    assert "мощность" not in joined


def test_enrich_from_web_returns_suggestion(monkeypatch):
    monkeypatch.setattr(enrich, "_search_ddgs", lambda query: [{
        "title": "Bosch GSB 550",
        "url": "https://example.com/gsb-550",
        "snippet": "Мощность 550 Вт. Вес без кабеля 1.8 кг. Частота вращения 2700 об/мин.",
    }])
    result = asyncio.run(enrich.enrich_from_web(
        "Дрель Bosch GSB 550",
        ["Мощность", "Масса"],
    ))
    assert result is not None
    assert result["brand"] == "Bosch"
    assert result["model"] == "GSB 550"
    assert result["facts"]
    assert result["suggestedDescription"].startswith("Дрель Bosch GSB 550.")
    assert "1.8 кг" in result["suggestedDescription"] or "2700" in result["suggestedDescription"]


def test_enrich_from_web_skips_without_identity(monkeypatch):
    called = []
    monkeypatch.setattr(enrich, "_search_ddgs", lambda query: called.append(query) or [])
    assert asyncio.run(enrich.enrich_from_web("Молоток деревянный 500 г")) is None
    assert called == []


def test_enrich_from_web_skips_brand_without_model(monkeypatch):
    called = []
    monkeypatch.setattr(enrich, "_search_ddgs", lambda query: called.append(query) or [])
    assert asyncio.run(enrich.enrich_from_web("Шуруповерт makita")) is None
    assert called == []


def test_identity_from_product_uses_accepted_fields():
    product = {
        "manufacturer": {"status": "accepted", "value": "Makita"},
        "model": {"status": "accepted", "value": "6827"},
        "article": {"status": "missing", "value": None},
    }
    identity = enrich.identity_from_product(product, "Сетевой шуруповерт")
    assert identity == {"brand": "Makita", "model": "6827", "kind": "шуруповёрт"}


def test_identity_from_product_falls_back_to_regex():
    product = {
        "manufacturer": {"status": "missing", "value": None},
        "model": {"status": "missing", "value": None},
        "article": {"status": "missing", "value": None},
    }
    identity = enrich.identity_from_product(product, "Шуруповерт makita 6827")
    assert identity == {"brand": "Makita", "model": "6827", "kind": "шуруповёрт"}


def test_enrich_numeric_tool_model(monkeypatch):
    monkeypatch.setattr(enrich, "_search_ddgs", lambda query: [{
        "title": "Шуруповерт Makita 6827 обзор характеристик",
        "url": "https://example.com/6827",
        "snippet": (
            "Мощность 570 Вт, максимальный крутящий момент 10 Н·м, "
            "вес 1.8 кг, макс. число оборотов 2500 об/мин."
        ),
    }])
    result = asyncio.run(enrich.enrich_from_web("Шуруповерт makita 6827"))
    assert result is not None
    assert result["model"] == "6827"
    joined = " | ".join(result["facts"]).casefold()
    assert "570" in joined and "вт" in joined
    assert "1.8" in joined or "1,8" in joined


def test_tool_enrichment_ignores_accessory_listings(monkeypatch):
    monkeypatch.setattr(enrich, "_search_ddgs", lambda query: [
        {
            "title": "Ограничитель глубины 10 6827 Makita 322120-3",
            "url": "https://example.com/bit",
            "snippet": "Количество в упаковке - 1 шт. Описание - Для бит 50 мм. Размер - 10 мм. Бренд - Makita.",
        },
        {
            "title": "Шуруповерт Makita 6827",
            "url": "https://example.com/6827",
            "snippet": "мощность 570 Вт, вес 1.8 кг, крутящий момент 10 Н·м",
        },
    ])
    result = asyncio.run(enrich.enrich_from_web("Шуруповерт makita 6827"))
    assert result is not None
    joined = " | ".join(result["facts"]).casefold()
    assert "570" in joined
    assert "бит" not in joined
    assert "упаковке" not in joined
