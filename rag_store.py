"""Disposable local FTS index. MySQL remains the source of truth."""

from __future__ import annotations

import hashlib
import heapq
import json
import re
import sqlite3
from contextlib import closing
from functools import lru_cache
from pathlib import Path
from typing import Any

INDEX_PATH = Path(__file__).resolve().parent / "data" / "rag-index.sqlite3"
MODEL_NAME = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
MODEL_CACHE = Path(__file__).resolve().parent / "data" / "embedding-models"


def connect(path: Path = INDEX_PATH) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=15)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("""CREATE TABLE IF NOT EXISTS documents (
        doc_key TEXT PRIMARY KEY, kind TEXT NOT NULL, title TEXT, url TEXT,
        source TEXT, published_at TEXT, metadata TEXT, content_hash TEXT NOT NULL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS chunks (
        id INTEGER PRIMARY KEY, doc_key TEXT NOT NULL, chunk_no INTEGER NOT NULL,
        text TEXT NOT NULL, FOREIGN KEY(doc_key) REFERENCES documents(doc_key))""")
    if not any(row[1] == "page_no" for row in db.execute("PRAGMA table_info(chunks)")):
        db.execute("ALTER TABLE chunks ADD COLUMN page_no INTEGER")
    db.execute("CREATE INDEX IF NOT EXISTS idx_chunks_document ON chunks(doc_key)")
    db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS chunk_search USING fts5(text, chunk_id UNINDEXED, tokenize='trigram')")
    db.execute("""CREATE TABLE IF NOT EXISTS chunk_vectors (
        chunk_id INTEGER PRIMARY KEY, model TEXT NOT NULL, vector BLOB NOT NULL)""")
    return db


def split_chunks(content: str, size: int = 1200, overlap: int = 120) -> list[str]:
    content = re.sub(r"\s+", " ", content).strip()
    if not content:
        return []
    result = []
    start = 0
    while start < len(content):
        end = min(start + size, len(content))
        if end < len(content):
            boundary = content.rfind(" ", start + size // 2, end)
            if boundary > start:
                end = boundary
        result.append(content[start:end].strip())
        if end == len(content):
            break
        start = max(start + 1, end - overlap)
    return result


def upsert_documents(items: list[dict[str, Any]], path: Path = INDEX_PATH,
                     prune: bool = False) -> dict[str, int]:
    inserted = 0
    unchanged = 0
    keys: set[str] = set()
    with closing(connect(path)) as db:
        for item in items:
            kind = str(item.get("kind") or "")
            if kind not in {"news", "clip", "social", "paper"} or not item.get("id"):
                continue
            doc_key = f"{kind}:{item['id']}"
            keys.add(doc_key)
            pages = item.get("pages") if kind == "paper" else None
            content = ("\f".join(str(page) for page in pages) if isinstance(pages, list)
                       else str(item.get("content") or "")).strip()
            if not content:
                continue
            metadata = {key: item.get(key) for key in ("author", "authors", "year", "doi", "source_url", "language", "country", "sentiment", "likes", "reposts", "replies") if key in item}
            digest = hashlib.sha256(json.dumps(item, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")).hexdigest()
            old = db.execute("SELECT content_hash FROM documents WHERE doc_key=?", (doc_key,)).fetchone()
            if old and old[0] == digest:
                unchanged += 1
                continue
            ids = [row[0] for row in db.execute("SELECT id FROM chunks WHERE doc_key=?", (doc_key,))]
            db.executemany("DELETE FROM chunk_search WHERE rowid=?", [(key,) for key in ids])
            db.executemany("DELETE FROM chunk_vectors WHERE chunk_id=?", [(key,) for key in ids])
            db.execute("DELETE FROM chunks WHERE doc_key=?", (doc_key,))
            db.execute("""INSERT INTO documents(doc_key,kind,title,url,source,published_at,metadata,content_hash)
                VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(doc_key) DO UPDATE SET
                title=excluded.title,url=excluded.url,source=excluded.source,
                published_at=excluded.published_at,metadata=excluded.metadata,content_hash=excluded.content_hash""",
                (doc_key, kind, item.get("title") or "", item.get("url") or "", item.get("source") or "",
                 item.get("published_at") or "", json.dumps(metadata, ensure_ascii=False), digest))
            numbered_chunks = ([(page_no, chunk) for page_no, page in enumerate(pages, 1)
                                for chunk in split_chunks(str(page), size=450, overlap=55)] if isinstance(pages, list)
                               else [(None, chunk) for chunk in split_chunks(content)])
            for number, (page_no, chunk) in enumerate(numbered_chunks):
                cursor = db.execute("INSERT INTO chunks(doc_key,chunk_no,text,page_no) VALUES(?,?,?,?)", (doc_key, number, chunk, page_no))
                db.execute("INSERT INTO chunk_search(rowid,text,chunk_id) VALUES(?,?,?)", (cursor.lastrowid, chunk, cursor.lastrowid))
            inserted += 1
        removed = 0
        if prune:
            for row in db.execute("SELECT doc_key FROM documents").fetchall():
                if row[0] in keys:
                    continue
                ids = [item[0] for item in db.execute("SELECT id FROM chunks WHERE doc_key=?", (row[0],))]
                db.executemany("DELETE FROM chunk_search WHERE rowid=?", [(identifier,) for identifier in ids])
                db.executemany("DELETE FROM chunk_vectors WHERE chunk_id=?", [(identifier,) for identifier in ids])
                db.execute("DELETE FROM chunks WHERE doc_key=?", (row[0],))
                db.execute("DELETE FROM documents WHERE doc_key=?", (row[0],))
                removed += 1
        db.commit()
    return {"indexed": inserted, "unchanged": unchanged, "removed": removed}


def _terms(query: str) -> list[str]:
    terms: list[str] = []
    for token in re.findall(r"[A-Za-z0-9]{3,}|[\u3400-\u9fff]+|[\u3040-\u30ff]+|[\uac00-\ud7af]+", query.casefold()):
        if re.search(r"[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af]", token) and len(token) > 4:
            terms.extend(token[i:i + 3] for i in range(len(token) - 2))
        elif len(token) >= 3:
            terms.append(token)
    return list(dict.fromkeys(terms))[:16]


def search(query: str, limit: int = 12, path: Path = INDEX_PATH,
           kinds: tuple[str, ...] = ("news", "clip", "social", "paper")) -> list[dict[str, Any]]:
    terms = _terms(query)
    if not terms:
        return []
    quoted = ['"' + term.replace('"', '""') + '"' for term in terms]
    if not kinds or any(kind not in {"news", "clip", "social", "paper"} for kind in kinds):
        return []
    placeholders = ",".join("?" for _ in kinds)
    with closing(connect(path)) as db:
        sql = f"""SELECT d.*, c.id AS chunk_id, c.text, c.page_no, bm25(chunk_search) AS score
            FROM chunk_search JOIN chunks c ON c.id=chunk_search.rowid
            JOIN documents d ON d.doc_key=c.doc_key
            WHERE chunk_search MATCH ? AND d.kind IN ({placeholders}) ORDER BY score LIMIT ?"""
        rows = db.execute(sql, (" AND ".join(quoted), *kinds, max(50, limit * 8))).fetchall()
        if len(rows) < limit:
            rows += db.execute(sql, (" OR ".join(quoted), *kinds, max(50, limit * 8))).fetchall()
    found = []
    seen: set[str] = set()
    for row in rows:
        if row["doc_key"] in seen:
            continue
        seen.add(row["doc_key"])
        found.append({"id": row["doc_key"], "chunk_id": row["chunk_id"], "kind": row["kind"], "title": row["title"],
                      "url": row["url"], "source": row["source"], "published_at": row["published_at"],
                      "excerpt": row["text"], "page_no": row["page_no"],
                      "metadata": json.loads(row["metadata"] or "{}")})
        if len(found) >= limit:
            break
    return found


def stats(path: Path = INDEX_PATH) -> dict[str, int]:
    with closing(connect(path)) as db:
        counts = {row[0]: row[1] for row in db.execute("SELECT kind,COUNT(*) FROM documents GROUP BY kind")}
        chunks = db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        vectors = db.execute("SELECT COUNT(*) FROM chunk_vectors").fetchone()[0]
        news_social_vectors = db.execute("""SELECT COUNT(*) FROM chunk_vectors v
            JOIN chunks c ON c.id=v.chunk_id JOIN documents d ON d.doc_key=c.doc_key
            WHERE d.kind IN ('news','clip','social') AND v.model=?""", (MODEL_NAME,)).fetchone()[0]
        news_social_chunks = db.execute("""SELECT COUNT(*) FROM chunks c JOIN documents d ON d.doc_key=c.doc_key
            WHERE d.kind IN ('news','clip','social')""").fetchone()[0]
    return {"news": counts.get("news", 0), "clip": counts.get("clip", 0),
            "social": counts.get("social", 0), "paper": counts.get("paper", 0),
            "chunks": chunks, "paper_vectors": vectors - news_social_vectors,
            "news_social_vectors": news_social_vectors, "news_social_chunks": news_social_chunks}


@lru_cache(maxsize=1)
def _embedding_model():
    from fastembed import TextEmbedding
    MODEL_CACHE.mkdir(parents=True, exist_ok=True)
    return TextEmbedding(model_name=MODEL_NAME, cache_dir=str(MODEL_CACHE), threads=4)


def paper_vector_status(paper_id: str, path: Path = INDEX_PATH) -> dict[str, int | bool]:
    with closing(connect(path)) as db:
        row = db.execute("""SELECT COUNT(*), COUNT(v.chunk_id) FROM chunks c
            LEFT JOIN chunk_vectors v ON v.chunk_id=c.id AND v.model=?
            WHERE c.doc_key=?""", (MODEL_NAME, f"paper:{paper_id}")).fetchone()
    return {"total": row[0], "ready": row[1], "complete": bool(row[0] and row[0] == row[1])}


def vectorize_paper(paper_id: str, progress=None, path: Path = INDEX_PATH) -> dict[str, int | bool]:
    import numpy as np
    with closing(connect(path)) as db:
        rows = db.execute("""SELECT c.id,c.text FROM chunks c
            LEFT JOIN chunk_vectors v ON v.chunk_id=c.id AND v.model=?
            WHERE c.doc_key=? AND v.chunk_id IS NULL ORDER BY c.chunk_no""",
            (MODEL_NAME, f"paper:{paper_id}")).fetchall()
    if not rows:
        return paper_vector_status(paper_id, path)
    model = _embedding_model()
    for offset in range(0, len(rows), 16):
        batch = rows[offset:offset + 16]
        vectors = list(model.embed([row["text"] for row in batch], batch_size=16))
        with closing(connect(path)) as db:
            for row, vector in zip(batch, vectors):
                array = np.asarray(vector, dtype=np.float32)
                norm = np.linalg.norm(array)
                if norm:
                    array /= norm
                db.execute("INSERT OR REPLACE INTO chunk_vectors(chunk_id,model,vector) VALUES(?,?,?)",
                           (row["id"], MODEL_NAME, array.tobytes()))
            db.commit()
        if progress:
            progress(min(offset + len(batch), len(rows)), len(rows))
    return paper_vector_status(paper_id, path)


def vectorize_news_social(progress=None, path: Path = INDEX_PATH) -> dict[str, int | bool]:
    """Fill missing vectors after SQL sync; never block a news/social ingest request."""
    import numpy as np
    with closing(connect(path)) as db:
        rows = db.execute("""SELECT c.id,c.text FROM chunks c
            JOIN documents d ON d.doc_key=c.doc_key
            LEFT JOIN chunk_vectors v ON v.chunk_id=c.id AND v.model=?
            WHERE d.kind IN ('news','clip','social') AND v.chunk_id IS NULL ORDER BY c.id""",
            (MODEL_NAME,)).fetchall()
    if not rows:
        status = stats(path)
        return {"total": status["news_social_chunks"], "ready": status["news_social_vectors"], "complete": True}
    model = _embedding_model()
    for offset in range(0, len(rows), 16):
        batch = rows[offset:offset + 16]
        vectors = list(model.embed([row["text"] for row in batch], batch_size=16))
        with closing(connect(path)) as db:
            for row, vector in zip(batch, vectors):
                array = np.asarray(vector, dtype=np.float32)
                norm = np.linalg.norm(array)
                if norm:
                    array /= norm
                db.execute("INSERT OR REPLACE INTO chunk_vectors(chunk_id,model,vector) VALUES(?,?,?)",
                           (row["id"], MODEL_NAME, array.tobytes()))
            db.commit()
        if progress:
            progress(offset + len(batch), len(rows))
    status = stats(path)
    return {"total": status["news_social_chunks"], "ready": status["news_social_vectors"],
            "complete": status["news_social_chunks"] == status["news_social_vectors"]}


def search_hybrid(query: str, limit: int = 12, path: Path = INDEX_PATH,
                  kinds: tuple[str, ...] = ("news", "clip", "social")) -> list[dict[str, Any]]:
    """Fuse lexical and multilingual semantic ranks; fall back to FTS when vectors are absent."""
    import numpy as np
    if not kinds or any(kind not in {"news", "clip", "social"} for kind in kinds):
        return []
    lexical = search(query, limit=max(30, limit * 4), path=path, kinds=kinds)
    placeholders = ",".join("?" for _ in kinds)
    with closing(connect(path)) as db:
        rows = db.execute(f"""SELECT d.*,c.id AS chunk_id,c.text,c.page_no,v.vector FROM chunk_vectors v
            JOIN chunks c ON c.id=v.chunk_id JOIN documents d ON d.doc_key=c.doc_key
            WHERE d.kind IN ({placeholders}) AND v.model=?""", (*kinds, MODEL_NAME)).fetchall()
    if not rows:
        return lexical[:limit]
    try:
        query_vector = np.asarray(next(iter(_embedding_model().embed([query]))), dtype=np.float32)
        norm = np.linalg.norm(query_vector)
        if norm:
            query_vector /= norm
    except Exception as error:
        print(f"News/social vector query failed: {type(error).__name__}")
        return lexical[:limit]
    candidates = []
    for row in rows:
        stored = np.frombuffer(row["vector"], dtype=np.float32)
        if stored.shape != query_vector.shape:
            continue
        score = float(np.dot(query_vector, stored))
        if score >= 0.28:
            candidates.append((score, row["chunk_id"], row))
    semantic = []
    for score, _, row in heapq.nlargest(max(50, limit * 8), candidates):
        semantic.append({"id": row["doc_key"], "chunk_id": row["chunk_id"], "kind": row["kind"],
                         "title": row["title"], "url": row["url"], "source": row["source"],
                         "published_at": row["published_at"], "excerpt": row["text"],
                         "page_no": row["page_no"], "metadata": json.loads(row["metadata"] or "{}"),
                         "similarity": round(score, 3)})
    fused: dict[str, tuple[float, dict[str, Any]]] = {}
    for weight, items in ((1.0, lexical), (1.0, semantic)):
        for rank, item in enumerate(items, 1):
            key = item["id"]
            score = weight / (60 + rank)
            old_score, old_item = fused.get(key, (0.0, item))
            preferred = item if item.get("similarity", 0) > old_item.get("similarity", 0) else old_item
            fused[key] = (old_score + score, preferred)
    return [item for _, item in sorted(fused.values(), key=lambda value: value[0], reverse=True)[:limit]]


def search_papers(query: str, limit: int = 5, path: Path = INDEX_PATH) -> list[dict[str, Any]]:
    import numpy as np
    lexical = search(query, limit=limit, path=path, kinds=("paper",))
    with closing(connect(path)) as db:
        rows = db.execute("""SELECT d.*,c.id AS chunk_id,c.text,c.page_no,v.vector FROM chunk_vectors v
            JOIN chunks c ON c.id=v.chunk_id JOIN documents d ON d.doc_key=c.doc_key
            WHERE d.kind='paper' AND v.model=?""", (MODEL_NAME,)).fetchall()
    if not rows:
        return lexical
    try:
        vector = np.asarray(next(iter(_embedding_model().embed([query]))), dtype=np.float32)
        norm = np.linalg.norm(vector)
        if norm:
            vector /= norm
    except Exception as error:
        print(f"Paper vector query failed: {type(error).__name__}")
        return lexical
    ranked = []
    for row in rows:
        stored = np.frombuffer(row["vector"], dtype=np.float32)
        if stored.shape != vector.shape:
            continue
        score = float(np.dot(vector, stored))
        if score >= 0.22:
            ranked.append((score, row))
    ranked.sort(key=lambda value: value[0], reverse=True)
    result = []
    seen: set[int] = set()
    seen_pages: set[tuple[str, int | None]] = set()
    per_paper: dict[str, int] = {}
    for score, row in ranked:
        page_key = (row["doc_key"], row["page_no"])
        if per_paper.get(row["doc_key"], 0) >= 2 or page_key in seen_pages:
            continue
        seen.add(row["chunk_id"])
        seen_pages.add(page_key)
        per_paper[row["doc_key"]] = per_paper.get(row["doc_key"], 0) + 1
        result.append({"id": row["doc_key"], "chunk_id": row["chunk_id"], "kind": "paper",
                       "title": row["title"], "url": row["url"], "source": row["source"],
                       "published_at": row["published_at"], "excerpt": row["text"],
                       "page_no": row["page_no"], "metadata": json.loads(row["metadata"] or "{}"),
                       "similarity": round(score, 3)})
        if len(result) >= limit:
            break
    for item in lexical:
        page_key = (item["id"], item["page_no"])
        if item["chunk_id"] not in seen and page_key not in seen_pages and per_paper.get(item["id"], 0) < 2 and len(result) < limit:
            seen_pages.add(page_key)
            per_paper[item["id"]] = per_paper.get(item["id"], 0) + 1
            result.append(item)
    return result
