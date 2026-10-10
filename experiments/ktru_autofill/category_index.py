"""Offline, atomic KTRU indexing and read-only category-level hybrid retrieval."""
from __future__ import annotations

from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
from typing import Callable, Iterable
import unicodedata

import numpy as np

MODEL = "google/embeddinggemma-2"
QUERY_PROMPT = "task: search result | query: "
_VERSION = 2
_RRF_K = 60


class CategoryIndexError(RuntimeError):
    """The offline index or its embedding configuration is unavailable/invalid."""


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _normalize(text):
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def _readonly(path):
    db = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    db.execute("PRAGMA query_only=ON")
    db.execute("PRAGMA cache_size=-8192")
    return db


def _card_text(card):
    parts = [str(card.get(key) or "").strip() for key in ("name", "modelName", "description")]
    for characteristic in card.get("skuCharacteristics") or []:
        definition = characteristic.get("productCharacteristicValue") or {}
        name = definition.get("productCharacteristicName")
        values = [str(characteristic[key]) for key in (
            "characteristicValueStringValue", "characteristicValueDecimalValue",
            "characteristicValueBoolValue") if characteristic.get(key) is not None]
        if name:
            unit = definition.get("productCharacteristicUnitName") or ""
            parts.append(f"{name}: {'; '.join(values)} {unit}".strip())
    return "\n".join(part for part in parts if part)


def _split_utf8(text, budget):
    """Lossless slices: every source character is represented, including long tokens."""
    current, size = [], 0
    for char in text:
        width = len(char.encode("utf-8"))
        if size + width > budget:
            # Prefer a whitespace boundary so lexical model numbers survive chunking.
            boundary = next((i + 1 for i in range(len(current) - 1, -1, -1)
                             if current[i].isspace()), len(current))
            yield "".join(current[:boundary])
            current = current[boundary:]
            size = len("".join(current).encode("utf-8"))
        current.append(char)
        size += width
    if current:
        yield "".join(current)


def _chunks(name, code, body, budget):
    # Full labels/path/characteristics remain in body and the relational metadata.
    # Repeat a bounded structural prefix without ever clipping the source body.
    label = f"title: {name} | KTRU: {code} | text: "
    prefix = next(_split_utf8(label, budget // 3))
    for chunk in _split_utf8(body, budget - len(prefix.encode("utf-8"))):
        yield prefix + chunk


def _embedding_matrix(value, count, dimension=None, *, normalized=False):
    vectors = np.asarray(value, dtype=np.float32)
    if (vectors.ndim != 2 or vectors.shape[0] != count or vectors.shape[1] == 0
            or (dimension is not None and vectors.shape[1] != dimension)
            or not np.isfinite(vectors).all()):
        raise CategoryIndexError("Invalid embedding dimensions or non-finite vectors")
    # Float64 norms avoid float32 overflow for malformed/very large vectors.
    norms = np.linalg.norm(vectors.astype(np.float64), axis=1)
    if np.any(norms <= 0) or not np.isfinite(norms).all():
        raise CategoryIndexError("Embedding vectors must have finite, nonzero norms")
    if normalized:
        if not np.allclose(norms, 1, atol=1e-3, rtol=0):
            raise CategoryIndexError("Stored embedding vectors are not L2-normalized")
        return vectors
    return (vectors.astype(np.float64) / norms[:, None]).astype(np.float32)


def _check_config(embedder, metadata):
    for attribute, key in (("model", "model"), ("model_name", "model"), ("model_id", "model"),
                           ("query_prompt", "query_prompt"), ("prompt", "query_prompt"),
                           ("max_document_bytes", "max_document_bytes"),
                           ("dimension", "dimension"), ("dimensions", "dimension"),
                           ("embedding_dimension", "dimension")):
        value = getattr(embedder, attribute, None)
        if value is not None and key in metadata and value != metadata[key]:
            raise CategoryIndexError(f"Embedding configuration mismatch: {attribute}")


def _ranks(order, scores):
    """Equal evidence receives equal rank; stable ordering alone must not add evidence."""
    ranks, previous, rank = {}, None, 0
    for position, item in enumerate(order, 1):
        value = scores[item]
        if previous is None or value != previous:
            rank = position
        ranks[item], previous = rank, value
    return ranks


def _stage_cards(db, cards, stats):
    """Disk-backed deduplication sees late conflicts without retaining the corpus in RAM."""
    db.execute("PRAGMA temp_store=FILE")
    db.execute("PRAGMA cache_size=-8192")
    db.execute("""CREATE TABLE candidates (
        fingerprint TEXT PRIMARY KEY, code TEXT, name_key TEXT NOT NULL,
        source_key TEXT NOT NULL, example TEXT NOT NULL) WITHOUT ROWID""")
    sql = """INSERT INTO candidates VALUES (?,?,?,?,?)
        ON CONFLICT(fingerprint) DO UPDATE SET
        code=CASE WHEN candidates.code=excluded.code THEN candidates.code ELSE NULL END,
        source_key=min(candidates.source_key, excluded.source_key),
        example=CASE WHEN excluded.source_key<candidates.source_key
                     THEN excluded.example ELSE candidates.example END"""
    for card in cards:
        stats["cards_seen"] += 1
        if "production_code" in ((card.get("_dataset") or {}).get("conflicting_fields") or []):
            stats["rejected_conflicts"] += 1
            continue
        code = str((card.get("production") or {}).get("code") or "").strip()
        name = str(card.get("name") or "").strip()
        text = _card_text(card)
        if not code or not text:
            stats["rejected_empty"] += 1
            continue
        # Unknown catalog labels still participate in conflict detection.
        fingerprint = hashlib.sha256(_json([_normalize(name), _normalize(text)]).encode()).hexdigest()
        example = {"id": card.get("id"), "name": name, "text": text}
        source_key = _json([card.get("id"), example])
        db.execute(sql, (fingerprint, code, _normalize(name), source_key, _json(example)))
        if stats["cards_seen"] % 10000 == 0:
            db.commit()
    db.commit()
    stats["conflicting_documents"] = db.execute(
        "SELECT count(*) FROM candidates WHERE code IS NULL").fetchone()[0]
    stats["unique_documents"] = db.execute("SELECT count(*) FROM candidates").fetchone()[0]
    db.execute("CREATE INDEX candidate_order ON candidates(code, fingerprint)")


def _sample_cards(db, categories, per_category):
    # Hash ordering is stable across corpus ordering; one title per category yields diversity.
    # The cursor streams disk data and the only live title set is bounded by per_category.
    current, names = None, set()
    for code, name_key, example in db.execute(
            "SELECT code,name_key,example FROM candidates WHERE code IS NOT NULL ORDER BY code,fingerprint"):
        if code not in categories:
            continue
        if current != code:
            current, names = code, set()
        if len(names) < per_category and name_key not in names:
            names.add(name_key)
            yield code, json.loads(example)


class HybridCategoryIndex:
    """Immutable vectors; each search opens its own fresh read-only SQLite connection.

    ``embed_query`` receives raw query text and must implement the stored query prompt.
    Available model/model_name, query_prompt, max_document_bytes and dimension attributes
    are checked against metadata. No model loading, building or lexical-only fallback occurs.
    """

    @classmethod
    def build(cls, path, nodes: Iterable[dict], cards: Iterable[dict],
              embed_documents: Callable[[list[str]], np.ndarray], *, per_category: int = 20,
              batch_size: int = 32, model: str = MODEL, query_prompt: str = QUERY_PROMPT,
              max_document_bytes: int = 1000) -> dict:
        if per_category < 0 or batch_size <= 0 or max_document_bytes < 64:
            raise ValueError("per_category >= 0, batch_size > 0 and max_document_bytes >= 64 required")
        path = Path(path).resolve()
        categories = {}
        for node in nodes:
            code, name = str(node["code"]).strip(), str(node["name"]).strip()
            if not code or not name or code in categories:
                raise CategoryIndexError("Catalog codes must be unique with nonempty names")
            characteristics = list(dict.fromkeys(node.get("characteristics") or []))
            categories[code] = {"code": code, "name": name, "path": list(node.get("path") or []),
                                "characteristics": characteristics, "fields": node.get("fields") or [],
                                "okpd2": node.get("okpd2"), "examples": []}
        if not categories:
            raise CategoryIndexError("Cannot build an empty category catalog")
        for category in categories.values():
            if not category["path"]:
                category["path"] = [category["name"]]
        stats = {"categories": len(categories), "examples": 0, "documents": 0,
                 "cards_seen": 0, "rejected_conflicts": 0, "rejected_empty": 0}
        metadata = {"version": _VERSION, "catalog": "ktru", "model": model, "query_prompt": query_prompt,
                    "max_document_bytes": max_document_bytes, "per_category": per_category}
        _check_config(embed_documents, metadata)
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".category-index-", dir=path.parent) as directory:
            with closing(sqlite3.connect(Path(directory) / "staging.sqlite")) as staging:
                _stage_cards(staging, cards, stats)
                for code, example in _sample_cards(staging, categories, per_category):
                    categories[code]["examples"].append(example)
                    stats["examples"] += 1
            output = Path(directory) / "index.sqlite"
            with closing(sqlite3.connect(output)) as db:
                db.executescript("""
                    CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                    CREATE TABLE categories (code TEXT PRIMARY KEY, payload TEXT NOT NULL);
                    CREATE TABLE documents (id INTEGER PRIMARY KEY, code TEXT NOT NULL,
                        kind TEXT NOT NULL, example_index INTEGER, chunk INTEGER NOT NULL,
                        text TEXT NOT NULL, vector BLOB NOT NULL);
                    CREATE INDEX documents_code ON documents(code);
                    CREATE VIRTUAL TABLE document_fts USING fts5(text, tokenize='unicode61 remove_diacritics 2');
                """)
                pending = []
                dimension = None

                def flush():
                    nonlocal dimension
                    if not pending:
                        return
                    vectors = _embedding_matrix(embed_documents([row[4] for row in pending]),
                                                len(pending), dimension)
                    dimension = vectors.shape[1]
                    for row, vector in zip(pending, vectors, strict=True):
                        doc_id = stats["documents"] + 1
                        db.execute("INSERT INTO documents VALUES (?,?,?,?,?,?,?)",
                                   (doc_id, *row, vector.astype("<f4", copy=False).tobytes()))
                        db.execute("INSERT INTO document_fts(rowid,text) VALUES (?,?)", (doc_id, row[4]))
                        stats["documents"] = doc_id
                    pending.clear()
                    db.commit()

                for code in sorted(categories):
                    category = categories[code]
                    db.execute("INSERT INTO categories VALUES (?,?)", (code, _json(category)))
                    body = (f"Category: {category['name']}\nPath: {' / '.join(category['path'])}\n"
                            f"Characteristics: {', '.join(category['characteristics'])}")
                    sources = [("category", None, category["name"], body)]
                    sources.extend(("ste", i, example["name"], example["text"])
                                   for i, example in enumerate(category["examples"]))
                    for kind, example_index, title, text in sources:
                        for chunk, document in enumerate(_chunks(title, code, text, max_document_bytes)):
                            pending.append((code, kind, example_index, chunk, document))
                            if len(pending) == batch_size:
                                flush()
                flush()
                metadata |= {"dimension": dimension, "categories": len(categories),
                             "documents": stats["documents"]}
                db.executemany("INSERT INTO metadata VALUES (?,?)", [(key, _json(value))
                                                                      for key, value in metadata.items()])
                db.execute("INSERT INTO document_fts(document_fts) VALUES ('optimize')")
                db.commit()
                if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise CategoryIndexError("Built category index failed SQLite validation")
            # The old index remains untouched on any earlier failure, including embedding errors.
            os.replace(output, path)
        return stats | {"dimension": dimension, "model": model, "query_prompt": query_prompt,
                        "max_document_bytes": max_document_bytes}

    @classmethod
    def load(cls, path, embed_query: Callable[[str], np.ndarray]):
        path = Path(path).resolve()
        try:
            with closing(_readonly(path)) as db:
                if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise CategoryIndexError("Category index failed SQLite validation")
                metadata = {key: json.loads(value) for key, value in db.execute("SELECT key,value FROM metadata")}
                if (metadata["version"] != _VERSION or metadata["catalog"] != "ktru"
                        or not isinstance(metadata["dimension"], int)
                        or metadata["dimension"] <= 0 or not isinstance(metadata["model"], str)
                        or not metadata["model"] or not isinstance(metadata["query_prompt"], str)
                        or not isinstance(metadata["max_document_bytes"], int)
                        or metadata["max_document_bytes"] < 64):
                    raise CategoryIndexError("Invalid category index metadata")
                _check_config(embed_query, metadata)
                categories = {code: json.loads(payload) for code, payload in
                              db.execute("SELECT code,payload FROM categories ORDER BY code")}
                if not categories or len(categories) != metadata["categories"]:
                    raise CategoryIndexError("Missing category metadata")
                for code, category in categories.items():
                    if (category["code"] != code or not isinstance(category["name"], str)
                            or not isinstance(category["path"], list)
                            or not all(isinstance(part, str) for part in category["path"])
                            or not isinstance(category["characteristics"], list)
                            or not all(isinstance(part, str) for part in category["characteristics"])
                            or not isinstance(category["examples"], list)):
                        raise CategoryIndexError("Malformed category metadata")
                    for example in category["examples"]:
                        if ("id" not in example or not isinstance(example["name"], str)
                                or not isinstance(example["text"], str)):
                            raise CategoryIndexError("Malformed example metadata")
                count, dimension = metadata["documents"], metadata["dimension"]
                if not isinstance(count, int) or count < len(categories):
                    raise CategoryIndexError("Invalid document count")
                if (db.execute("SELECT count(*) FROM documents").fetchone()[0] != count
                        or db.execute("SELECT count(*) FROM document_fts").fetchone()[0] != count
                        or db.execute("""SELECT count(*) FROM documents d LEFT JOIN document_fts f
                                         ON f.rowid=d.id WHERE f.rowid IS NULL OR f.text<>d.text""").fetchone()[0]):
                    raise CategoryIndexError("Missing documents or lexical index")
                codes = list(categories)
                code_indices = {code: i for i, code in enumerate(codes)}
                vectors = np.empty((count, dimension), dtype=np.float32)
                document_categories = np.empty(count, dtype=np.int32)
                document_examples = np.full(count, -1, dtype=np.int32)
                seen_categories = set()
                for position, (doc_id, code, kind, example, chunk, text, blob) in enumerate(db.execute(
                        "SELECT id,code,kind,example_index,chunk,text,vector FROM documents ORDER BY id")):
                    if (doc_id != position + 1 or code not in categories
                            or kind not in ("category", "ste") or not isinstance(chunk, int) or chunk < 0
                            or not isinstance(text, str) or not text
                            or len(text.encode("utf-8")) > metadata["max_document_bytes"]
                            or not isinstance(blob, bytes) or len(blob) != dimension * 4):
                        raise CategoryIndexError("Malformed index document/vector")
                    if kind == "category":
                        if example is not None:
                            raise CategoryIndexError("Malformed category document")
                        seen_categories.add(code)
                    else:
                        if not isinstance(example, int) or not 0 <= example < len(categories[code]["examples"]):
                            raise CategoryIndexError("Malformed example reference")
                        document_examples[position] = example
                    vectors[position] = np.frombuffer(blob, dtype="<f4")
                    document_categories[position] = code_indices[code]
                if seen_categories != set(categories):
                    raise CategoryIndexError("Some catalog categories have no documents")
                for start in range(0, count, 4096):
                    block = vectors[start:start + 4096]
                    _embedding_matrix(block, len(block), dimension, normalized=True)
                for array in (vectors, document_categories, document_examples):
                    array.setflags(write=False)
        except CategoryIndexError:
            raise
        except (sqlite3.Error, OSError, ValueError, TypeError, KeyError, OverflowError) as exc:
            raise CategoryIndexError(f"Cannot load category index {path}: {exc}") from exc
        instance = cls()
        instance.path, instance.embed_query = path, embed_query
        instance.metadata, instance.categories, instance.codes = metadata, categories, codes
        instance.vectors = vectors
        instance._document_categories, instance._document_examples = document_categories, document_examples
        instance.families = {}
        for code, category in categories.items():
            key = (_normalize(category['name']), category['okpd2'])
            instance.families.setdefault(key, []).append(code)
        return instance

    def variants(self, code: str) -> list[dict]:
        category = self.categories[code]
        codes = self.families[(_normalize(category['name']), category['okpd2'])]
        return [self.categories[c] for c in codes]

    def search(self, text: str, limit: int = 50) -> list[dict]:
        if not isinstance(text, str) or not text.strip() or limit <= 0:
            raise ValueError("A nonempty query and positive limit are required")
        _check_config(self.embed_query, self.metadata)
        raw_query = self.embed_query(text)
        try:
            query = np.asarray(raw_query, dtype=np.float32)
            if query.ndim != 1:
                raise CategoryIndexError("Query embedding must be a one-dimensional vector")
            query = _embedding_matrix(query[None, :], 1, self.metadata["dimension"])[0]
        except CategoryIndexError:
            raise
        except (ValueError, TypeError, RuntimeError) as exc:
            raise CategoryIndexError(f"Cannot embed category query: {exc}") from exc
        similarities = self.vectors @ query
        category_scores = np.full(len(self.codes), -np.inf, dtype=np.float32)
        np.maximum.at(category_scores, self._document_categories, similarities)
        # Category ranks use the best chunk, not counts/votes from large STE categories.
        semantic_order = sorted(range(len(self.codes)), key=lambda i: (-float(category_scores[i]), self.codes[i]))
        semantic_ranks = {self.codes[i]: rank for i, rank in _ranks(semantic_order, category_scores).items()}
        lexical_scores, example_lexical = {}, {}
        terms = list(dict.fromkeys(re.findall(r"[^\W_]+", _normalize(text))))
        expression = " OR ".join('"' + term + '"' for term in terms)
        try:
            with closing(_readonly(self.path)) as db:
                if expression:
                    rows = db.execute("""SELECT d.code,d.example_index,bm25(document_fts),d.id
                        FROM document_fts JOIN documents d ON d.id=document_fts.rowid
                        WHERE document_fts MATCH ? ORDER BY bm25(document_fts),d.id""", (expression,))
                    for code, example, score, _ in rows:
                        lexical_scores.setdefault(code, float(score))
                        if example is not None:
                            example_lexical.setdefault((code, example), float(score))
        except (sqlite3.Error, OSError) as exc:
            raise CategoryIndexError(f"Cannot query category index {self.path}: {exc}") from exc
        lexical_order = sorted(lexical_scores, key=lambda code: (lexical_scores[code], code))
        lexical_ranks = _ranks(lexical_order, lexical_scores)
        scores = {code: 1 / (_RRF_K + semantic_ranks[code]) +
                  (1 / (_RRF_K + lexical_ranks[code]) if code in lexical_ranks else 0)
                  for code in self.codes}
        ranked = []
        seen_families = set()
        for code in sorted(self.codes, key=lambda code: (-scores[code], code)):
            family = (_normalize(self.categories[code]['name']), self.categories[code]['okpd2'])
            if family in seen_families:
                continue
            seen_families.add(family)
            ranked.append(code)
            if len(ranked) == limit:
                break
        results = []
        for code in ranked:
            category = self.categories[code]
            category_index = self.codes.index(code)
            example_scores = np.full(len(category["examples"]), -np.inf, dtype=np.float32)
            mask = ((self._document_categories == category_index) & (self._document_examples >= 0))
            np.maximum.at(example_scores, self._document_examples[mask], similarities[mask])
            semantic_examples = sorted(range(len(example_scores)), key=lambda i: (-float(example_scores[i]), i))
            example_ranks = _ranks(semantic_examples, example_scores)
            lexical_examples = sorted((i for i in range(len(example_scores)) if (code, i) in example_lexical),
                                      key=lambda i: (example_lexical[code, i], i))
            lexical_example_ranks = _ranks(lexical_examples,
                                           {i: example_lexical[code, i] for i in lexical_examples})
            relevant = sorted(example_ranks, key=lambda i: (
                -(1 / (_RRF_K + example_ranks[i]) + (1 / (_RRF_K + lexical_example_ranks[i])
                                                   if i in lexical_example_ranks else 0)), i))[:3]
            selected_examples = []
            for i in relevant:
                example = dict(category["examples"][i])
                # Do not send entire long catalog documents to the LLM. Retrieve the
                # relevant evidence chunk (including a lexical hit near the tail).
                if len(example["text"].encode("utf-8")) > self.metadata["max_document_bytes"]:
                    example_mask = mask & (self._document_examples == i)
                    positions = np.flatnonzero(example_mask)
                    best = min(positions, key=lambda p: (-float(similarities[p]), int(p)))
                    with closing(_readonly(self.path)) as db:
                        if (code, i) in example_lexical:
                            fragment = db.execute("""SELECT d.text FROM document_fts
                                JOIN documents d ON d.id=document_fts.rowid
                                WHERE document_fts MATCH ? AND d.code=? AND d.example_index=?
                                ORDER BY bm25(document_fts),d.id LIMIT 1""", (expression, code, i)).fetchone()
                        else:
                            fragment = db.execute("SELECT text FROM documents WHERE id=?", (int(best) + 1,)).fetchone()
                    example["text"] = fragment[0]
                selected_examples.append(example)
            results.append({"code": code, "name": category["name"], "path": list(category["path"]),
                            "characteristics": list(category["characteristics"]),
                            "fields": category["fields"], "okpd2": category["okpd2"],
                            "examples": selected_examples,
                            "score": float(scores[code]),
                            "signals": ["semantic"] + (["lexical"] if code in lexical_ranks else [])})
        return results
