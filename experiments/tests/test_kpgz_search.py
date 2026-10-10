import importlib.util
import json
from pathlib import Path

import numpy as np
import pytest

from ktru_autofill.kpgz_search import KpgzSearch


def catalog():
    return [
        {"code": "01", "name": "Мебель"},
        {"code": "01.01", "name": "Диваны", "kpgzCharacteristics": [{"characteristicName": "Высота"}]},
        {"code": "01.02", "name": "Столы", "kpgzCharacteristics": [{"characteristicName": "Ширина"}]},
    ]


class Encoder:
    def __init__(self):
        self.calls = []

    def encode(self, texts, **kwargs):
        self.calls.append((texts, kwargs))
        return np.array([[1., 0.] if "Диван" in t else [0., 1.] for t in texts], dtype=np.float32)


def test_search_normalized_vectors_and_prompts(tmp_path, monkeypatch):
    model = Encoder()
    monkeypatch.setattr(KpgzSearch, "_load_model", lambda _: model)
    search = KpgzSearch(catalog(), cache=tmp_path / "index.npz")
    assert len(search.nodes) == 2
    assert "Мебель / Диваны" in search.documents[0]
    assert search.documents[0].startswith("title: Диваны | text:")
    ranked = search.search_many(["Диван мягкий", "Стол офисный"], top_k=2)
    assert [r[0]["code"] for r in ranked] == ["01.01", "01.02"]
    assert ranked[0][0]["score"] == 1
    assert model.calls[0][1]["prompt"] == ""
    assert model.calls[1][1]["prompt"] == "task: search result | query: "
    assert all(c[1]["normalize_embeddings"] for c in model.calls)
    node = search.node(ranked[0])
    assert node["_retrieval"]["top_k"] == ranked[0]
    assert "_retrieval" not in search.nodes[0]


def test_renormalizes_model_output_in_float32(tmp_path, monkeypatch):
    class RoundedEncoder(Encoder):
        def encode(self, texts, **kwargs):
            return super().encode(texts, **kwargs) * 0.9985

    monkeypatch.setattr(KpgzSearch, "_load_model", lambda _: RoundedEncoder())
    search = KpgzSearch(catalog(), cache=tmp_path / "index.npz")
    np.testing.assert_allclose(np.linalg.norm(search.vectors, axis=1), 1)
    assert search.search("Диван")[0]["score"] == 1


def test_cache_reuse_and_invalidation(tmp_path, monkeypatch):
    model = Encoder()
    monkeypatch.setattr(KpgzSearch, "_load_model", lambda _: model)
    cache = tmp_path / "index.npz"
    KpgzSearch(catalog(), cache=cache)
    assert len(model.calls) == 1
    cached = KpgzSearch(catalog(), cache=cache)
    assert len(model.calls) == 1
    assert cached._model is None
    changed = catalog()
    changed[1]["name"] = "Диваны модульные"
    KpgzSearch(changed, cache=cache)
    assert len(model.calls) == 2
    KpgzSearch(changed, cache=cache, max_length=256)
    assert len(model.calls) == 3


def test_kpgz_cli_search_does_not_lookup_supplied_category(monkeypatch, capsys):
    spec = importlib.util.spec_from_file_location(
        "kpgz_to_jev", Path(__file__).resolve().parents[1] / "experiments" / "kpgz_to_jev.py")
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)

    class Search:
        def search(self, text, top_k):
            assert text == "Диван 830 мм"
            return [{"code": "01.01", "name": "Диваны", "score": 0.9}]

        def node(self, ranked):
            return catalog()[1] | {"kpgzCharacteristics": [{"characteristicId": 1,
                "characteristicName": "Высота", "conditionTypeId": script.NUMBER}], "_retrieval": {"top_k": ranked}}

    monkeypatch.setattr(script, "from_args", lambda nodes, args: Search())
    monkeypatch.setattr(script, "load_okei", lambda _: {})
    monkeypatch.setattr(script, "find_node", lambda *args: pytest.fail("Не должен читать готовый код КПГЗ"))
    script.main(["--search-category", "--text", "Диван 830 мм"])
    out = json.loads(capsys.readouterr().out)
    assert out["kpgz"]["code"] == "01.01"
    assert out["category_search"]["top_k"][0]["score"] == 0.9
    with pytest.raises(SystemExit):
        script.main(["01.01", "--search-category", "--text", "Диван 830 мм"])


def test_invalid_search_inputs_and_vectors(tmp_path, monkeypatch):
    with pytest.raises(ValueError, match="схемами"):
        KpgzSearch([], cache=tmp_path / "none.npz")
    monkeypatch.setattr(KpgzSearch, "_load_model", lambda _: Encoder())
    search = KpgzSearch(catalog(), cache=tmp_path / "index.npz")
    with pytest.raises(ValueError, match="top_k"):
        search.search("Диван", 0)
    with pytest.raises(ValueError, match="непустой"):
        search.search(" ")
    assert search.search_many([]) == []
    with pytest.raises(ValueError, match="Некорректные"):
        search._validate(np.array([[np.nan, 0]]), 1)
    with pytest.raises(ValueError, match="L2"):
        search._validate(np.array([[0., 0.]]), 1)


def neighbor_search(tmp_path, monkeypatch):
    from ktru_autofill.kpgz_search import SteNeighbors
    monkeypatch.setattr(KpgzSearch, "_load_model", lambda _: Encoder())
    search = KpgzSearch(catalog(), cache=tmp_path / "index.npz")
    corpus = [{"id": 1, "code": "01.01", "name": "Диван угловой", "text": "Диван угловой"},
              {"id": 2, "code": "01.01", "name": "Диван прямой", "text": "Диван прямой"},
              {"id": 3, "code": "01.02", "name": "Стол письменный", "text": "Стол письменный"}]
    search.neighbors = SteNeighbors(search, corpus, cache=tmp_path / "ste.npz", k=3)
    return search


def test_neighbor_vote_ranks_categories_with_examples(tmp_path, monkeypatch):
    search = neighbor_search(tmp_path, monkeypatch)
    ranked = search.search("Диван", top_k=5)
    assert [c["code"] for c in ranked] == ["01.01", "01.02"]
    assert [n["id"] for n in ranked[0]["neighbors"]] == [1, 2]
    assert abs(sum(c["score"] for c in ranked) - 1) < 1e-6
    node = search.node(ranked)
    assert node["_retrieval"]["method"] == "ste_neighbors"
    assert node["_retrieval"]["neighbors"]["k"] == 3


def test_jev_chooses_only_among_neighbor_categories(tmp_path, monkeypatch):
    from ktru_autofill.kpgz_search import jev_category
    search = neighbor_search(tmp_path, monkeypatch)
    ranked = search.search("Диван", top_k=5)
    calls = []

    def call(request):
        calls.append(request)
        return {"answers": {"category": {"choice": "Столы", "confidence": .8,
                                         "probabilities": {"Диваны": .1, "Столы": .9}}}, "usage": {}}

    state = {"товар": "Стол-диван", "категория": "не для Jev", "описание": ""}
    result = jev_category(search, state, ranked, call)
    question = calls[0]["questions"]["category"]
    assert set(question["criteria"]) == {"Диваны", "Столы"}
    assert question["criteria"]["Диваны"]["раздел КПГЗ"] == "Мебель / Диваны"
    assert question["criteria"]["Диваны"]["похожие карточки этой категории"] == ["Диван угловой", "Диван прямой"]
    assert calls[0]["state"] == {"товар": "Стол-диван", "описание": "нет"}
    assert [c["code"] for c in result["top_k"]] == ["01.02", "01.01"]
    assert result["top_k"][0]["jev"] == .9
    # Тот же запрос из лога — без вызова API; без call — только запрос.
    again = jev_category(search, state, ranked, lambda r: pytest.fail("повторный вызов"), result)
    assert again["top_k"] == result["top_k"]
    assert jev_category(search, state, ranked, None)["response"] is None


def test_neighbor_corpus_rejects_unknown_categories(tmp_path, monkeypatch):
    from ktru_autofill.kpgz_search import SteNeighbors
    monkeypatch.setattr(KpgzSearch, "_load_model", lambda _: Encoder())
    search = KpgzSearch(catalog(), cache=tmp_path / "index.npz")
    with pytest.raises(ValueError, match="без схемы"):
        SteNeighbors(search, [{"id": 1, "code": "09", "name": "x", "text": "x"}], cache=tmp_path / "s.npz")
    with pytest.raises(ValueError, match="пуст"):
        SteNeighbors(search, [], cache=tmp_path / "s.npz")


def five_categories(tmp_path, monkeypatch):
    search = neighbor_search(tmp_path, monkeypatch)
    names = ["Диваны", "Столы"]
    return search, [{"code": c, "name": n, "score": .5, "neighbors": [{"id": i, "name": f"{n} {i}", "score": .9}]}
                    for i, (c, n) in enumerate(zip(["01.01", "01.02"], names))]


def test_noul_ranks_candidates_by_independent_yes(tmp_path, monkeypatch):
    from ktru_autofill.kpgz_search import jev_category_noul
    search, cands = five_categories(tmp_path, monkeypatch)
    calls = []

    def call(request):
        calls.append(request)
        return {"answers": {"c0": {"type": "noul", "noul": .2}, "c1": {"type": "noul", "noul": .7}},
                "usage": {"input_tokens": 5, "output_tokens": 2}}

    result = jev_category_noul(search, {"товар": "Стол"}, cands, call)
    q = calls[0]["questions"]["c0"]
    assert q["type"] == "noul" and q["instructions"]["категория"] == "Диваны"
    assert q["instructions"]["раздел КПГЗ"] == "Мебель / Диваны"
    assert [c["code"] for c in result["top_k"]] == ["01.02", "01.01"]
    assert result["confidence"] == .7 and result["response"]["usage"]["input_tokens"] == 5
    again = jev_category_noul(search, {"товар": "Стол"}, cands, None, result)
    assert again["top_k"] == result["top_k"]
    assert jev_category_noul(search, {"товар": "Стол"}, cands, None)["response"] is None


def test_knockout_asks_groups_then_final_among_winners():
    from ktru_autofill.kpgz_search import jev_category_knockout

    class Search:
        paths = {f"0{i}": f"Раздел / К{i}" for i in range(7)}

    cands = [{"code": f"0{i}", "name": f"К{i}", "neighbors": []} for i in range(7)]  # группы 5 + 2
    calls = []

    def call(request):
        calls.append(request)
        answers = {}
        for name, q in request["questions"].items():
            labels = list(q["criteria"])
            best = labels[-1]  # всегда последний вариант
            answers[name] = {"type": "choice", "choice": best, "confidence": .9,
                             "probabilities": {l: (.9 if l == best else .1 / (len(labels) - 1)) for l in labels}}
        return {"answers": answers, "usage": {"input_tokens": 1, "output_tokens": 1}}

    result = jev_category_knockout(Search(), {"товар": "x"}, cands, call)
    assert len(calls) == 2
    assert [len(q["criteria"]) for q in calls[0]["questions"].values()] == [5, 2]
    assert list(calls[1]["questions"]["final"]["criteria"]) == ["К4", "К6"]
    assert [c["code"] for c in result["top_k"][:2]] == ["06", "04"]
    assert result["response"]["usage"] == {"input_tokens": 2, "output_tokens": 2}
    # Кеш: повтор без вызовов; без call — запросы первого шага, ответа нет.
    assert jev_category_knockout(Search(), {"товар": "x"}, cands, None, result)["top_k"] == result["top_k"]
    dry = jev_category_knockout(Search(), {"товар": "x"}, cands, None)
    assert dry["response"] is None and len(dry["calls"]) == 1
