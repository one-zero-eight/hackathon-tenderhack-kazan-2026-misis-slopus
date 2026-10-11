import asyncio
import struct
import threading
from contextlib import asynccontextmanager
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from search_api import classifier_search as classifiers
from search_api.classifier_search import build_classifier_index, fold_text, search_classifiers


@pytest.fixture
def classifier_fixture(tmp_path, monkeypatch):
    okpd = tmp_path / "okpd.parquet"
    ktru = tmp_path / "ktru.parquet"
    moy = tmp_path / "moy.parquet"
    kpgz = tmp_path / "kpgz.parquet"
    spgz = tmp_path / "spgz.parquet"
    pq.write_table(pa.Table.from_pylist([
        {
            "code": "25.73.30.141", "name": "Молотки", "parent": None, "level": 4,
            "is_actual": True, "similar_products": ["молоток", "молоток слесарный"],
        },
        {
            "code": "10.81.12", "name": "Сахар", "parent": None, "level": 3,
            "is_actual": True, "similar_products": ["сахар", "сахарный песок"],
        },
    ]), okpd)
    pq.write_table(pa.Table.from_pylist([
        {
            "code": "25.73.30.141-00000012", "name": "Молоток", "status": "ACTIVE",
            "is_template": False, "okpd2": {"code": "25.73.30.141", "name": "Молотки"},
            "parent": {"code": "25.73.30.141-00000011"},
        },
        {
            "code": "25.73.30.141-00000007", "name": "Молоток", "status": "ACTIVE",
            "is_template": False, "okpd2": {"code": "25.73.30.141", "name": "Молотки"},
            "parent": {"code": "25.73.30.141-00000011"},
        },
    ]), ktru)
    pq.write_table(pa.Table.from_pylist([
        {
            "code": "25.73.30.141-00000012", "tree_display_name": "Молоток слесарный",
            "shortname": "Молоток слесарный",
            "similar_products": ["молоток", "молоток слесарный", "молоток штукатурный"],
        },
        {
            "code": "25.73.30.141-00000007", "tree_display_name": "Молоток штукатурный",
            "shortname": "Молоток штукатурный",
            "similar_products": ["молоток", "молоток слесарный", "молоток штукатурный"],
        },
    ]), moy)
    pq.write_table(pa.Table.from_pylist([
        {
            "code": "01.02.03", "name": "Молотки ручные", "externalId": 1001,
            "parentId": None, "treePath": ".1.2.", "okpd": {"code": "25.73.30.141"},
            "isLastLevel": True,
        },
    ]), kpgz)
    pq.write_table(pa.Table.from_pylist([
        {
            "id": 555, "name": "Молоток слесарный 500 г", "kpgzId": 1001,
            "okpd2Code": "25.73.30.141", "okpdCode": None, "isExcluded": 0,
        },
    ]), spgz)
    output = tmp_path / "classifier-search.sqlite"
    counts = build_classifier_index(
        output=output,
        okpd_path=okpd,
        ktru_path=ktru,
        moy_ktru_path=moy,
        kpgz_path=kpgz,
        spgz_path=spgz,
    )
    assert counts["ktru"] == 2
    monkeypatch.setattr("search_api.classifier_search.settings.classifier_database", output)
    return output


def test_classifier_search_accepts_long_description(classifier_fixture):
    query = "Молоток слесарный со стальным бойком. " * 40
    assert len(query) > 500
    result = asyncio.run(search_classifiers(query=query, mode="text"))
    assert result["query"] == query.strip()
    with pytest.raises(ValueError, match="20000"):
        asyncio.run(search_classifiers(query="а" * 20_001, mode="text"))


def test_fold_text_normalizes_yo():
    assert fold_text("Ёлка") == fold_text("елка")


def test_classifier_search_prefers_exact_display_name(classifier_fixture):
    result = asyncio.run(search_classifiers(query="Молоток штукатурный", mode="text", limit=5))
    assert result["okpdKtru"]
    top = result["okpdKtru"][0]
    assert top["ktru"]["code"] == "25.73.30.141-00000007"
    assert top["ktru"]["name"] == "Молоток штукатурный"
    assert top["evidence"] in {"exact_name", "name", "synonym"}


def test_classifier_search_finds_sugar_without_products(classifier_fixture):
    result = asyncio.run(search_classifiers(query="сахар", mode="text", limit=5))
    assert any(pair["okpd2"] and "Сахар" in pair["okpd2"]["name"] for pair in result["okpdKtru"])


def test_classifier_search_pairs_spgz_with_kpgz(classifier_fixture):
    result = asyncio.run(search_classifiers(query="Молоток слесарный 500", mode="text", limit=5))
    assert result["kpgzSpgz"]
    top = result["kpgzSpgz"][0]
    assert top["spgz"]["code"] == "555"
    assert top["kpgz"]["code"] == "01.02.03"


def _search(query, **kwargs):
    return asyncio.run(search_classifiers(query=query, mode=kwargs.pop("mode", "text"), **kwargs))


def _rebuild_fixture(output, *, publish=True):
    folder = output.parent
    return build_classifier_index(output=output, okpd_path=folder / "okpd.parquet",
                                  ktru_path=folder / "ktru.parquet", moy_ktru_path=folder / "moy.parquet",
                                  kpgz_path=folder / "kpgz.parquet", spgz_path=folder / "spgz.parquet",
                                  publish=publish)


def _rewrite(path, update):
    rows = pq.read_table(path).to_pylist()
    update(rows)
    pq.write_table(pa.Table.from_pylist(rows), path)


@pytest.mark.parametrize("query", ["ШТУКАТУРНЫЙ МОЛОТОК", "штукатурными молотками"])
def test_reordered_inflected_names_are_confident(classifier_fixture, query):
    pair = _search(query)["okpdKtru"][0]
    assert pair["ktru"]["code"].endswith("00000007")
    assert pair["evidence"] == "exact_name"
    assert pair["confident"] and not pair["needsChoice"]
    assert pair["reason"] is None
    assert pair["ktru"]["score"] is None  # lexical evidence is not a probability


def test_official_names_remain_searchable_and_ambiguous(classifier_fixture):
    result = _search("Молоток", limit=1)
    pair = result["okpdKtru"][0]
    assert pair["evidence"] == "exact_name"
    assert pair["needsChoice"] and pair["ambiguous"]
    assert pair["reason"] == "ambiguous_classifier"
    assert result["needsChoice"]  # ambiguity survives result truncation


def test_exact_display_name_beats_shared_synonyms(classifier_fixture):
    pair = _search("молоток слесарный")["okpdKtru"][0]
    assert pair["ktru"]["code"].endswith("00000012")
    assert pair["confident"] and not pair["ambiguous"]
    assert not _search("молоток слесарный")["needsChoice"]  # weak other-family context doesn't veto a direct name


def test_exact_code_wins_and_has_schema_fields(classifier_fixture):
    result = _search("25.73.30.141-00000007")
    pair = result["okpdKtru"][0]
    assert pair["confident"]
    assert pair["evidence"] == "exact_code"
    assert {"kind", "code", "name", "linkCode", "status", "isTemplate", "isExcluded", "score",
            "rank", "match", "evidence", "synonyms", "weakSynonyms"} <= result["hits"][0].keys()


def test_modest_typo_requires_choice(classifier_fixture):
    pair = _search("молток штукатурный")["okpdKtru"][0]
    assert pair["ktru"]["code"].endswith("00000007")
    assert pair["reason"] == "typo_match"
    assert not pair["confident"]


def test_corpus_yo_fold_matches_query(classifier_fixture):
    _rewrite(classifier_fixture.parent / "moy.parquet",
             lambda rows: rows[0].update(tree_display_name="Молоток тяжёлый", shortname="Молоток тяжёлый"))
    _rebuild_fixture(classifier_fixture)
    pair = _search("тяжелыми молотками")["okpdKtru"][0]
    assert pair["ktru"]["code"].endswith("00000012") and pair["confident"]


def test_weak_context_is_not_a_direct_synonym(classifier_fixture):
    _rewrite(classifier_fixture.parent / "okpd.parquet",
             lambda rows: rows[0]["similar_products"].append("ручной ударный инструмент"))
    _rebuild_fixture(classifier_fixture)
    result = _search("ручной ударный инструмент")
    pair = result["kpgzSpgz"][0]
    assert pair["evidence"] == "weak_synonym"
    assert not pair["confident"] and pair["needsChoice"]
    assert not pair["kpgz"]["synonyms"]
    assert pair["kpgz"]["weakSynonyms"]


def test_unrelated_one_word_objects_do_not_match(classifier_fixture):
    def append(rows):
        row = dict(rows[0], code="99-00000001", name="Штукатурный раствор")
        rows.append(row)
    _rewrite(classifier_fixture.parent / "ktru.parquet", append)
    _rebuild_fixture(classifier_fixture)
    result = _search("Молоток штукатурный")
    assert not any(hit["code"] == "99-00000001" for hit in result["hits"])


def test_exact_names_in_different_branches_are_ambiguous(classifier_fixture):
    def append(rows):
        rows.append(dict(rows[0], code="10.81.12-00000001", name="Молоток штукатурный",
                         okpd2={"code": "10.81.12", "name": "Сахар"}))
    _rewrite(classifier_fixture.parent / "ktru.parquet", append)
    _rebuild_fixture(classifier_fixture)
    pair = _search("Молоток штукатурный", limit=1)["okpdKtru"][0]
    assert pair["ambiguous"] and pair["needsChoice"]


def test_inactive_entries_are_hits_not_automatic_pairs(classifier_fixture):
    _rewrite(classifier_fixture.parent / "ktru.parquet", lambda rows: rows[1].update(status="ARCHIVED"))
    _rebuild_fixture(classifier_fixture)
    result = _search("25.73.30.141-00000007")
    assert any(hit["code"].endswith("00000007") for hit in result["hits"])
    assert not any(pair["ktru"] and pair["ktru"]["code"].endswith("00000007") for pair in result["okpdKtru"])


def test_excluded_spgz_not_automatic(classifier_fixture):
    _rewrite(classifier_fixture.parent / "spgz.parquet", lambda rows: rows[0].update(isExcluded=1))
    _rebuild_fixture(classifier_fixture)
    result = _search("555")
    assert result["hits"] and not result["kpgzSpgz"]


def test_path_contains_parent_labels(classifier_fixture):
    conn = classifiers.open_classifier_db(classifier_fixture, readonly=True)
    try:
        row = conn.execute("SELECT path,embed_text FROM entries WHERE kind='spgz'").fetchone()
        assert "Молотки ручные" in row["path"]
        assert "Молотки ручные" in row["embed_text"]
        aliases = conn.execute("SELECT text FROM aliases a JOIN entries e ON e.id=a.entry_id WHERE e.kind='ktru' AND a.role='name'").fetchall()
        assert "Молоток" in {row["text"] for row in aliases}
    finally:
        conn.close()


@pytest.mark.parametrize("source", ["okpd", "ktru", "moy", "kpgz", "spgz"])
def test_every_source_change_rejected_without_rebuild(classifier_fixture, source):
    before = classifier_fixture.stat().st_mtime_ns
    path = classifier_fixture.parent / f"{source}.parquet"
    _rewrite(path, lambda rows: None)
    with pytest.raises(RuntimeError, match="Изменился источник"):
        _search("Молоток")
    assert classifier_fixture.stat().st_mtime_ns == before


def test_missing_optional_mirror_fingerprinted(classifier_fixture):
    mirror = classifier_fixture.parent / "moy.parquet"
    mirror.unlink()
    _rebuild_fixture(classifier_fixture)
    assert _search("Молоток")["hits"]
    pq.write_table(pa.Table.from_pylist([{"code": "new", "tree_display_name": "new"}]), mirror)
    with pytest.raises(RuntimeError, match="Изменился источник"):
        _search("Молоток")


@pytest.fixture
def deterministic_embeddings(monkeypatch):
    monkeypatch.setattr(classifiers.settings, "dimensions", 3)
    batches = []

    @asynccontextmanager
    async def client():
        yield None

    async def documents(texts, task, *, client=None):
        assert task == "document"
        batches.append(list(texts))
        return [struct.pack("<3f", *( (1.0, 0.0, 0.0) if "00000007" in text else (0.0, 1.0, 0.0))) for text in texts]

    async def query(_text):
        return struct.pack("<3f", 1.0, 0.0, 0.0)

    monkeypatch.setattr(classifiers, "embed_client", client)
    monkeypatch.setattr(classifiers, "embed_texts", documents)
    monkeypatch.setattr(classifiers, "embed_query", query)
    return batches


def test_plain_text_supported_but_hybrid_requires_complete_embeddings(classifier_fixture):
    assert _search("сахар")["hits"]
    with pytest.raises(RuntimeError, match="Эмбеддинги"):
        _search("сахар", mode="hybrid")


def test_keyset_embedding_resume_atomic_publication(classifier_fixture, deterministic_embeddings):
    original = classifier_fixture.read_bytes()
    _rebuild_fixture(classifier_fixture, publish=False)
    stage = classifiers.classifier_staging_path(classifier_fixture)
    assert asyncio.run(classifiers.embed_classifier_pending(path=stage, limit=3, batch_size=2)) == 3
    assert list(map(len, deterministic_embeddings)) == [2, 1]
    with pytest.raises(RuntimeError, match="Эмбеддинги"):
        classifiers.publish_classifier_index(output=classifier_fixture)
    assert classifier_fixture.read_bytes() == original
    assert asyncio.run(classifiers.embed_classifier_pending(path=stage, batch_size=2)) == 3
    assert list(map(len, deterministic_embeddings)) == [2, 1, 2, 1]
    classifiers.publish_classifier_index(output=classifier_fixture)
    assert not stage.exists()
    result = _search("неизвестное", mode="semantic")
    pair = result["okpdKtru"][0]
    assert pair["ktru"]["code"].endswith("00000007")
    assert pair["evidence"] == "semantic" and pair["confident"]
    assert pair["ktru"]["score"] == pytest.approx(1)
    assert pair["rank"] < 0.1  # RRF remains a rank, not a probability


def test_embedding_failure_leaves_old_publication(classifier_fixture, deterministic_embeddings, monkeypatch):
    original = classifier_fixture.read_bytes()
    _rebuild_fixture(classifier_fixture, publish=False)
    async def failure(*args, **kwargs):
        raise RuntimeError("embedding service failed")
    monkeypatch.setattr(classifiers, "embed_texts", failure)
    with pytest.raises(RuntimeError, match="service failed"):
        asyncio.run(classifiers.embed_classifier_pending(path=classifiers.classifier_staging_path(classifier_fixture)))
    assert classifier_fixture.read_bytes() == original
    assert classifiers.classifier_staging_path(classifier_fixture).exists()


@pytest.mark.parametrize("field,value", [("model_id", "different"), ("query_prompt", "different"),
                                         ("document_prompt", "different"), ("dimensions", 4)])
def test_embedding_signature_checked(classifier_fixture, deterministic_embeddings, monkeypatch, field, value):
    _rebuild_fixture(classifier_fixture)
    asyncio.run(classifiers.embed_classifier_pending(path=classifier_fixture, batch_size=2))
    monkeypatch.setattr(classifiers.settings, field, value)
    with pytest.raises(RuntimeError, match="модель или prompt"):
        _search("сахар", mode="hybrid")
    assert _search("сахар")["hits"]


def test_embedded_content_hash_stale_rejected(classifier_fixture, deterministic_embeddings):
    _rebuild_fixture(classifier_fixture)
    asyncio.run(classifiers.embed_classifier_pending(path=classifier_fixture, batch_size=2))
    conn = classifiers.open_classifier_db(classifier_fixture)
    with conn:
        conn.execute("UPDATE entries SET content_hash='changed' WHERE id=1")
    conn.close()
    with pytest.raises(RuntimeError, match="устарели"):
        _search("сахар", mode="hybrid")


def test_pair_confidence_uses_cosine_runner_not_rrf(classifier_fixture, deterministic_embeddings, monkeypatch):
    _rebuild_fixture(classifier_fixture)
    asyncio.run(classifiers.embed_classifier_pending(path=classifier_fixture, batch_size=2))
    async def equal_query(_text):
        return struct.pack("<3f", 1, 1, 0)
    monkeypatch.setattr(classifiers, "embed_query", equal_query)
    pair = _search("unknown", mode="semantic", limit=1)["okpdKtru"][0]
    assert pair["needsChoice"] and not pair["confident"]
    assert pair["reason"] == "ambiguous_classifier"


def test_missing_kpgz_link_cannot_be_confident(classifier_fixture):
    _rewrite(classifier_fixture.parent / "spgz.parquet", lambda rows: rows[0].update(kpgzId=9999))
    _rebuild_fixture(classifier_fixture)
    pair = _search("555")["kpgzSpgz"][0]
    assert pair["spgz"]["code"] == "555" and pair["kpgz"] is None
    assert not pair["confident"] and pair["reason"] == "incomplete_pair"


def test_source_changed_during_embedding_never_publish(classifier_fixture, deterministic_embeddings, monkeypatch):
    original = classifier_fixture.read_bytes()
    _rebuild_fixture(classifier_fixture, publish=False)
    embed = classifiers.embed_texts
    changed = False
    async def mutate(texts, task, *, client=None):
        nonlocal changed
        if not changed:
            _rewrite(classifier_fixture.parent / "moy.parquet", lambda rows: None)
            changed = True
        return await embed(texts, task, client=client)
    monkeypatch.setattr(classifiers, "embed_texts", mutate)
    with pytest.raises(RuntimeError, match="Изменился источник"):
        asyncio.run(classifiers.embed_classifier_pending(path=classifiers.classifier_staging_path(classifier_fixture), batch_size=2))
    assert classifier_fixture.read_bytes() == original


def test_casefolded_exact_synonym_lookup(classifier_fixture):
    pair = _search("САХАРНЫЙ ПЕСОК")["okpdKtru"][0]
    assert pair["okpd2"]["code"] == "10.81.12"
    assert pair["evidence"] == "synonym" and pair["needsChoice"]


def test_configured_path_changes_detected(classifier_fixture, monkeypatch):
    for attr, name in [("okpd2_parquet", "okpd"), ("ktru_parquet", "ktru"), ("moy_ktru_parquet", "moy"),
                       ("kpgz_parquet", "kpgz"), ("spgz_parquet", "spgz")]:
        monkeypatch.setattr(classifiers.settings, attr, classifier_fixture.parent / f"{name}.parquet")
    build_classifier_index(output=classifier_fixture)
    monkeypatch.setattr(classifiers.settings, "moy_ktru_parquet", classifier_fixture.parent / "different.parquet")
    with pytest.raises(RuntimeError, match="путь источника"):
        _search("Молоток")


def test_cli_custom_output_and_resume(classifier_fixture, deterministic_embeddings, monkeypatch):
    from search_api.cli import main
    for attr, name in [("okpd2_parquet", "okpd"), ("ktru_parquet", "ktru"), ("moy_ktru_parquet", "moy"),
                       ("kpgz_parquet", "kpgz"), ("spgz_parquet", "spgz")]:
        monkeypatch.setattr(classifiers.settings, attr, classifier_fixture.parent / f"{name}.parquet")
    custom = classifier_fixture.parent / "custom.sqlite"
    with pytest.raises(SystemExit) as first:
        main(["classifiers", "--output", str(custom), "--embed", "--limit", "2", "--batch", "1"])
    assert first.value.code == 0
    assert not custom.exists() and classifiers.classifier_staging_path(custom).exists()
    with pytest.raises(SystemExit) as resumed:
        main(["classifiers-embed", "--output", str(custom), "--batch", "2"])
    assert resumed.value.code == 0 and custom.exists()
    monkeypatch.setattr(classifiers.settings, "classifier_database", custom)
    assert _search("Молоток штукатурный", mode="hybrid")["okpdKtru"][0]["confident"]


@pytest.fixture
def tracked_connections(monkeypatch):
    connections = []
    connect = classifiers.sqlite3.connect

    class TrackedConnection(classifiers.sqlite3.Connection):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.open_thread = threading.get_ident()
            self.close_thread = None
            connections.append(self)

        def close(self):
            self.close_thread = threading.get_ident()
            super().close()

    def tracked_connect(*args, **kwargs):
        return connect(*args, **kwargs, factory=TrackedConnection)

    monkeypatch.setattr(classifiers.sqlite3, "connect", tracked_connect)
    return connections


def _assert_worker_connections_closed(connections, event_loop_thread):
    assert connections
    for conn in connections:
        assert conn.open_thread != event_loop_thread
        assert conn.close_thread == conn.open_thread
        with pytest.raises(classifiers.sqlite3.ProgrammingError, match="closed"):
            conn.execute("SELECT 1")


@pytest.mark.parametrize("hook", ["validate_classifier_index", "_exact_boosts", "_text_hits",
                                  "_semantic_hits", "_pair_okpd_ktru"])
def test_concurrent_search_workers_leave_event_loop_responsive(
        classifier_fixture, deterministic_embeddings, monkeypatch, hook, tracked_connections):
    # The semantic fixture returns immediately, like a cached query embedding.
    _rebuild_fixture(classifier_fixture)
    asyncio.run(classifiers.embed_classifier_pending(path=classifier_fixture, batch_size=2))
    tracked_connections.clear()
    original = getattr(classifiers, hook)
    entered = []
    release = threading.Event()
    lock = threading.Lock()
    loop_thread = threading.get_ident()
    mode = "hybrid" if hook == "_semantic_hits" else "text"
    first_calls = set()

    def blocked(conn, *args, **kwargs):
        assert threading.get_ident() != loop_thread
        # Validation occurs in both phases; only block each request's first phase.
        with lock:
            first = len(entered) < 2 and id(conn) not in first_calls
            if first:
                first_calls.add(id(conn))
                entered.append(conn)
        if first:
            assert release.wait(timeout=3), "event loop did not release concurrent workers"
        return original(conn, *args, **kwargs)

    monkeypatch.setattr(classifiers, hook, blocked)
    embed = classifiers.embed_query

    async def cached_embedding(query):
        assert threading.get_ident() == loop_thread
        return await embed(query)

    monkeypatch.setattr(classifiers, "embed_query", cached_embedding)

    async def run():
        tasks = [asyncio.create_task(search_classifiers(query="Молоток штукатурный", mode=mode))
                 for _ in range(2)]
        ticks = 0
        deadline = asyncio.get_running_loop().time() + 2
        try:
            while len(entered) < 2 and asyncio.get_running_loop().time() < deadline:
                ticks += 1
                await asyncio.sleep(0.001)
            assert len(entered) == 2
            assert entered[0] is not entered[1]
            assert ticks > 0
        finally:
            release.set()
            results = await asyncio.gather(*tasks)
        assert all(result["okpdKtru"][0]["confident"] for result in results)

    asyncio.run(run())
    assert len(tracked_connections) == 4
    _assert_worker_connections_closed(tracked_connections, loop_thread)


def test_cancelled_search_closes_inflight_worker_connection(classifier_fixture, monkeypatch, tracked_connections):
    entered = threading.Event()
    release = threading.Event()
    original = classifiers._text_hits

    def blocked(conn, query, limit):
        entered.set()
        assert release.wait(timeout=3)
        return original(conn, query, limit)

    monkeypatch.setattr(classifiers, "_text_hits", blocked)

    async def run():
        task = asyncio.create_task(search_classifiers(query="Молоток", mode="text"))
        deadline = asyncio.get_running_loop().time() + 2
        try:
            while not entered.is_set() and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.001)
            assert entered.is_set()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())  # Waits for the executor to finish its cancelled request's worker.
    assert len(tracked_connections) == 1
    _assert_worker_connections_closed(tracked_connections, threading.get_ident())


@pytest.mark.parametrize("hook", ["validate_classifier_index", "_exact_boosts", "_text_hits",
                                  "_semantic_hits", "_pair_okpd_ktru", "embed_query"])
def test_search_errors_close_worker_connections(
        classifier_fixture, deterministic_embeddings, monkeypatch, hook, tracked_connections):
    # Build before tracking search-owned connections.
    _rebuild_fixture(classifier_fixture)
    asyncio.run(classifiers.embed_classifier_pending(path=classifier_fixture, batch_size=2))
    tracked_connections.clear()
    loop_thread = threading.get_ident()

    def failure(*args, **kwargs):
        assert threading.get_ident() != loop_thread
        raise RuntimeError("search stage failed")

    async def embedding_failure(query):
        assert threading.get_ident() == loop_thread
        raise RuntimeError("search stage failed")

    monkeypatch.setattr(classifiers, hook, embedding_failure if hook == "embed_query" else failure)
    with pytest.raises(RuntimeError, match="search stage failed"):
        _search("Молоток", mode="hybrid")
    assert len(tracked_connections) == (2 if hook in {"_semantic_hits", "_pair_okpd_ktru"} else 1)
    _assert_worker_connections_closed(tracked_connections, loop_thread)


def test_connection_setup_error_closes_connection(classifier_fixture, monkeypatch, tracked_connections):
    def failure(conn):
        raise RuntimeError("extension setup failed")

    monkeypatch.setattr(classifiers.sqlite_vec, "load", failure)
    with pytest.raises(RuntimeError, match="extension setup failed"):
        _search("Молоток")
    _assert_worker_connections_closed(tracked_connections, threading.get_ident())


def test_invalid_index_rejected_before_remote_embedding(classifier_fixture, monkeypatch, tracked_connections):
    async def unexpected(query):
        raise AssertionError("Invalid index must be rejected before embedding")

    monkeypatch.setattr(classifiers, "embed_query", unexpected)
    with pytest.raises(RuntimeError, match="Эмбеддинги"):
        _search("Молоток", mode="hybrid")
    _assert_worker_connections_closed(tracked_connections, threading.get_ident())


def test_semantic_timing_includes_async_embedding(
        classifier_fixture, deterministic_embeddings, monkeypatch, caplog, tracked_connections):
    _rebuild_fixture(classifier_fixture)
    asyncio.run(classifiers.embed_classifier_pending(path=classifier_fixture, batch_size=2))
    tracked_connections.clear()
    embed = classifiers.embed_query
    loop_thread = threading.get_ident()

    async def delayed_embedding(query):
        assert threading.get_ident() == loop_thread
        await asyncio.sleep(0.04)
        return await embed(query)

    monkeypatch.setattr(classifiers, "embed_query", delayed_embedding)
    with caplog.at_level("INFO", logger="uvicorn.error"):
        result = _search("unknown", mode="semantic")
    record = next(record for record in caplog.records if record.msg.startswith("Classifier search mode="))
    timings = record.args[-1]
    assert timings["semantic"] >= 35
    assert result["durationMs"] >= timings["semantic"]
    assert {"setup", "exact", "text", "semantic", "ranking_pairs"} == timings.keys()
    assert len(tracked_connections) == 2
    _assert_worker_connections_closed(tracked_connections, loop_thread)
