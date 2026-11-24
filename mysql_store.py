"""Optional local MySQL storage for news evidence and research runs."""

from __future__ import annotations

import json
import hashlib
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse


ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
CONFIG_FILE = DATA_DIR / "mysql-config.json"
LEGACY_FILE = DATA_DIR / "records.json"


def canonical_url(url: str) -> str:
    parsed = urlparse(url)
    query = sorted((key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True)
                   if not key.lower().startswith(("utm_", "fbclid", "gclid")))
    return urlunparse((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path.rstrip("/") or "/", "", urlencode(query), ""))


def content_hash(content: str) -> str | None:
    normalized = re.sub(r"\s+", "", content).casefold()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest() if normalized else None


def url_hash(url: str) -> str:
    return hashlib.sha256(canonical_url(url).encode("utf-8")).hexdigest()


def article_versions(article: dict[str, Any]) -> list[tuple[str, str, str | None]]:
    versions = [(article["url"], "原始报道", None)]
    content_url = article.get("content_url")
    if content_url and canonical_url(content_url) != canonical_url(article["url"]):
        level = article.get("evidence_level") or ""
        kind = "公开快照" if "存档" in level else "公开转载" if "转载" in level else "取文版本"
        versions.append((content_url, kind, (article.get("archive") or {}).get("captured_at")))
    return versions


def upsert_article_master(cursor: Any, article: dict[str, Any], run_id: str) -> str:
    """Keep one full-text master while associating every run and public version."""
    origin_hash = url_hash(article["url"])
    digest = content_hash(str(article.get("content") or ""))
    cursor.execute("SELECT master_id FROM article_aliases WHERE url_hash=%s", (origin_hash,))
    row = cursor.fetchone()
    if not row and digest:
        cursor.execute("SELECT id FROM article_master WHERE content_hash=%s LIMIT 1", (digest,))
        row = cursor.fetchone()
    master_id = row[0] if row else uuid.uuid4().hex
    if not row:
        cursor.execute("""INSERT INTO article_master
            (id, title, origin_url, source, published_at, content, content_hash, first_run_id)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
            (master_id, article["title"], article["url"], article.get("source"),
             article.get("published_at"), article.get("content"), digest, run_id))
    elif article.get("content"):
        cursor.execute("""UPDATE article_master SET content=%s, content_hash=%s
            WHERE id=%s AND (content IS NULL OR content='')""",
            (article["content"], digest, master_id))
    for url, kind, captured_at in article_versions(article):
        cursor.execute("""INSERT IGNORE INTO article_aliases
            (url_hash, master_id, url, version_kind, captured_at, first_run_id)
            VALUES (%s,%s,%s,%s,%s,%s)""",
            (url_hash(url), master_id, url, kind, captured_at, run_id))
    article["global_article_id"] = master_id
    return master_id


def validate_config(value: dict[str, Any]) -> dict[str, Any]:
    host = str(value.get("host", "127.0.0.1")).strip()
    if host not in {"127.0.0.1", "localhost"}:
        raise ValueError("演示版仅允许连接本机 MySQL（127.0.0.1 或 localhost）。")
    try:
        port = int(value.get("port", 3306))
    except (TypeError, ValueError) as error:
        raise ValueError("端口必须是整数。") from error
    if not 1 <= port <= 65535:
        raise ValueError("端口必须在 1 到 65535 之间。")
    database = str(value.get("database", "suyuan")).strip()
    if not re.fullmatch(r"[A-Za-z0-9_]{1,64}", database):
        raise ValueError("数据库名只能包含英文字母、数字和下划线，最多 64 位。")
    user = str(value.get("user", "")).strip()
    if not user or len(user) > 128:
        raise ValueError("请填写 MySQL 用户名。")
    return {
        "host": host,
        "port": port,
        "database": database,
        "user": user,
        "password": str(value.get("password", "")),
    }


def load_config() -> dict[str, Any] | None:
    if not CONFIG_FILE.exists():
        return None
    return validate_config(json.loads(CONFIG_FILE.read_text(encoding="utf-8")))


def public_config() -> dict[str, Any]:
    config = load_config()
    if not config:
        return {"configured": False, "host": "127.0.0.1", "port": 3306, "database": "suyuan", "user": "", "password_set": False}
    return {**{key: config[key] for key in ("host", "port", "database", "user")}, "configured": True, "password_set": bool(config["password"])}


def connect(config: dict[str, Any], *, select_database: bool = True):
    try:
        import pymysql
    except ImportError as error:
        raise RuntimeError("缺少 PyMySQL。请运行 python -m pip install -r requirements.txt。") from error
    return pymysql.connect(
        host=config["host"], port=config["port"], user=config["user"],
        password=config["password"], database=config["database"] if select_database else None,
        charset="utf8mb4", connect_timeout=5, read_timeout=15, write_timeout=15,
        autocommit=False,
    )


def test_connection(config: dict[str, Any]) -> str:
    connection = connect(config, select_database=False)
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT VERSION()")
            return str(cursor.fetchone()[0])
    finally:
        connection.close()


def initialize_tables(config: dict[str, Any]) -> None:
    # The database identifier is validated above; parameters cannot stand for SQL identifiers.
    server = connect(config, select_database=False)
    try:
        with server.cursor() as cursor:
            cursor.execute(f"CREATE DATABASE IF NOT EXISTS `{config['database']}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
        server.commit()
    finally:
        server.close()
    connection = connect(config)
    try:
        with connection.cursor() as cursor:
            cursor.execute("""CREATE TABLE IF NOT EXISTS research_runs (
                id VARCHAR(32) PRIMARY KEY,
                created_at VARCHAR(40) NOT NULL,
                task TEXT NOT NULL,
                plan_json LONGTEXT NOT NULL,
                plan_mode VARCHAR(60) NOT NULL,
                assessment_json LONGTEXT NOT NULL,
                KEY idx_research_runs_created_at (created_at)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""")
            cursor.execute("""CREATE TABLE IF NOT EXISTS article_master (
                id VARCHAR(32) PRIMARY KEY,
                title TEXT NOT NULL,
                origin_url TEXT NOT NULL,
                source VARCHAR(255),
                published_at VARCHAR(64),
                content MEDIUMTEXT,
                content_hash CHAR(64),
                first_run_id VARCHAR(32) NOT NULL,
                KEY idx_article_master_content_hash (content_hash)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""")
            cursor.execute("""CREATE TABLE IF NOT EXISTS article_aliases (
                url_hash CHAR(64) PRIMARY KEY,
                master_id VARCHAR(32) NOT NULL,
                url TEXT NOT NULL,
                version_kind VARCHAR(32) NOT NULL,
                captured_at VARCHAR(32),
                first_run_id VARCHAR(32) NOT NULL,
                KEY idx_article_aliases_master (master_id),
                CONSTRAINT fk_article_alias_master FOREIGN KEY (master_id) REFERENCES article_master(id)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""")
            cursor.execute("""CREATE TABLE IF NOT EXISTS browser_clips (
                id VARCHAR(32) PRIMARY KEY,
                canonical_hash CHAR(64) NOT NULL UNIQUE,
                url TEXT NOT NULL,
                canonical_url TEXT NOT NULL,
                title TEXT NOT NULL,
                source VARCHAR(255),
                published_at VARCHAR(64),
                language VARCHAR(16),
                method VARCHAR(32) NOT NULL DEFAULT 'browser_visible',
                content MEDIUMTEXT NOT NULL,
                imported_at VARCHAR(40) NOT NULL,
                master_id VARCHAR(32),
                KEY idx_browser_clips_imported (imported_at),
                KEY idx_browser_clips_master (master_id)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""")
            cursor.execute("SHOW COLUMNS FROM browser_clips LIKE 'method'")
            if cursor.fetchone() is None:
                cursor.execute("ALTER TABLE browser_clips ADD COLUMN method VARCHAR(32) NOT NULL DEFAULT 'browser_visible'")
            cursor.execute("""CREATE TABLE IF NOT EXISTS social_posts (
                id VARCHAR(180) PRIMARY KEY,
                platform VARCHAR(32) NOT NULL,
                source_query VARCHAR(255),
                title TEXT,
                content TEXT NOT NULL,
                url TEXT NOT NULL,
                author VARCHAR(255),
                created_at VARCHAR(40),
                observed_at VARCHAR(40) NOT NULL,
                language VARCHAR(16),
                likes INT NOT NULL DEFAULT 0,
                reposts INT NOT NULL DEFAULT 0,
                replies INT NOT NULL DEFAULT 0,
                KEY idx_social_platform_date (platform, created_at)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""")
            cursor.execute("""CREATE TABLE IF NOT EXISTS paper_documents (
                id CHAR(32) PRIMARY KEY,
                file_hash CHAR(64) NOT NULL UNIQUE,
                filename VARCHAR(255) NOT NULL,
                title TEXT NOT NULL,
                authors TEXT,
                publication_year SMALLINT,
                doi VARCHAR(255),
                source_url TEXT,
                metadata_json LONGTEXT,
                page_count INT NOT NULL,
                text_chars INT NOT NULL,
                pages_json LONGTEXT NOT NULL,
                imported_at VARCHAR(40) NOT NULL,
                KEY idx_papers_year (publication_year)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""")
            cursor.execute("SHOW COLUMNS FROM paper_documents LIKE 'source_url'")
            if not cursor.fetchone():
                cursor.execute("ALTER TABLE paper_documents ADD COLUMN source_url TEXT AFTER doi")
            cursor.execute("SHOW COLUMNS FROM paper_documents LIKE 'metadata_json'")
            if not cursor.fetchone():
                cursor.execute("ALTER TABLE paper_documents ADD COLUMN metadata_json LONGTEXT AFTER source_url")
            cursor.execute("""CREATE TABLE IF NOT EXISTS news_articles (
                run_id VARCHAR(32) NOT NULL,
                article_id VARCHAR(16) NOT NULL,
                title TEXT NOT NULL,
                url TEXT NOT NULL,
                source VARCHAR(255),
                published_at VARCHAR(64),
                excerpt TEXT,
                content MEDIUMTEXT,
                evidence_level VARCHAR(32) NOT NULL,
                access_note TEXT,
                archive_json TEXT,
                content_url TEXT,
                genre VARCHAR(32),
                search_language VARCHAR(16),
                within_window TINYINT(1),
                sample_status VARCHAR(32),
                global_article_id VARCHAR(32),
                passages_json TEXT,
                selection_rank INT,
                representative_score FLOAT,
                rerank_score FLOAT,
                authority INT,
                search_provenance_json TEXT,
                PRIMARY KEY (run_id, article_id),
                CONSTRAINT fk_news_run FOREIGN KEY (run_id) REFERENCES research_runs(id)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""")
            cursor.execute("""CREATE TABLE IF NOT EXISTS evidence_reviews (
                run_id VARCHAR(32) NOT NULL,
                passage_id VARCHAR(32) NOT NULL,
                excerpt_hash CHAR(64) NOT NULL,
                status VARCHAR(16) NOT NULL,
                note TEXT,
                reviewed_at VARCHAR(40) NOT NULL,
                PRIMARY KEY (run_id, passage_id),
                KEY idx_evidence_reviews_run (run_id)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""")
            for name, definition in (
                ("search_language", "VARCHAR(16)"),
                ("within_window", "TINYINT(1)"),
                ("sample_status", "VARCHAR(32)"),
                ("content_url", "TEXT"),
                ("genre", "VARCHAR(32)"),
                ("global_article_id", "VARCHAR(32)"),
                ("passages_json", "TEXT"),
                ("selection_rank", "INT"),
                ("representative_score", "FLOAT"),
                ("rerank_score", "FLOAT"),
                ("authority", "INT"),
                ("search_provenance_json", "TEXT"),
            ):
                cursor.execute(f"SHOW COLUMNS FROM news_articles LIKE '{name}'")
                if cursor.fetchone() is None:
                    cursor.execute(f"ALTER TABLE news_articles ADD COLUMN {name} {definition}")
            cursor.execute("""SELECT run_id, article_id, title, url, source, published_at, content,
                evidence_level, archive_json, content_url, passages_json
                FROM news_articles WHERE global_article_id IS NULL""")
            legacy_rows = cursor.fetchall()
            for row in legacy_rows:
                article = {"title": row[2], "url": row[3], "source": row[4],
                           "published_at": row[5], "content": row[6], "evidence_level": row[7],
                           "archive": json.loads(row[8]) if row[8] else None, "content_url": row[9]}
                master_id = upsert_article_master(cursor, article, row[0])
                cursor.execute("""UPDATE news_articles SET global_article_id=%s,
                    content=NULL WHERE run_id=%s AND article_id=%s""", (master_id, row[0], row[1]))
        connection.commit()
    finally:
        connection.close()


def insert_run(run: dict[str, Any], config: dict[str, Any]) -> None:
    connection = connect(config)
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT IGNORE INTO research_runs (id, created_at, task, plan_json, plan_mode, assessment_json) VALUES (%s,%s,%s,%s,%s,%s)",
                (run["id"], run["created_at"], run["task"], json.dumps(run["plan"], ensure_ascii=False),
                 run["plan_mode"], json.dumps(run["assessment"], ensure_ascii=False)),
            )
            for article in run.get("candidates", run.get("articles", [])):
                master_id = upsert_article_master(cursor, article, run["id"])
                cursor.execute(
                    """INSERT IGNORE INTO news_articles
                    (run_id, article_id, title, url, source, published_at, excerpt, content, evidence_level, access_note, archive_json,
                     search_language, within_window, sample_status, content_url, genre, global_article_id, passages_json,
                     selection_rank, representative_score, rerank_score, authority, search_provenance_json)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (run["id"], article["id"], article["title"], article["url"], article.get("source"),
                     article.get("published_at"), article.get("excerpt"),
                     article.get("content") if article.get("browser_import_id") else None,
                     article["evidence_level"], article.get("access_note"),
                     json.dumps(article.get("archive"), ensure_ascii=False) if article.get("archive") else None,
                     article.get("search_language"), article.get("within_window"), article.get("sample_status"),
                     article.get("content_url") or article["url"], article.get("genre"), master_id,
                     json.dumps(article.get("passages", []), ensure_ascii=False),
                     article.get("selection_rank"), article.get("representative_score"),
                     article.get("rerank_score"), article.get("authority"),
                     json.dumps({"queries": article.get("search_queries", []),
                                 "languages": article.get("found_languages", []),
                                 "browser_import_id": article.get("browser_import_id")}, ensure_ascii=False)),
                )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def save_clip(item: dict[str, Any], config: dict[str, Any]) -> str:
    """Persist one browser import immediately, independently of research runs."""
    connection = connect(config)
    try:
        with connection.cursor() as cursor:
            master_id = upsert_article_master(cursor, item, f"clip-{item['id']}")
            canonical = canonical_url(item["canonical_url"] or item["url"])
            cursor.execute("""INSERT INTO browser_clips
                (id, canonical_hash, url, canonical_url, title, source, published_at,
                 language, method, content, imported_at, master_id)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON DUPLICATE KEY UPDATE url=VALUES(url), canonical_url=VALUES(canonical_url),
                title=VALUES(title), source=VALUES(source), published_at=VALUES(published_at),
                language=VALUES(language), method=VALUES(method), content=VALUES(content),
                imported_at=VALUES(imported_at), master_id=VALUES(master_id)""",
                (item["id"], url_hash(canonical), item["url"], canonical, item["title"],
                 item.get("source"), item.get("published_at"), item.get("language"),
                 item.get("method") or "browser_visible", item["content"], item["imported_at"], master_id))
        connection.commit()
        return master_id
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def list_clips(config: dict[str, Any], *, include_content: bool = False) -> list[dict[str, Any]]:
    connection = connect(config)
    try:
        with connection.cursor() as cursor:
            cursor.execute("""SELECT id, url, canonical_url, title, source, published_at,
                language, method, content, imported_at, master_id
                FROM browser_clips ORDER BY imported_at DESC, id DESC""")
            rows = cursor.fetchall()
        return [{"id": row[0], "url": row[1], "canonical_url": row[2], "title": row[3],
                 "source": row[4], "published_at": row[5], "language": row[6],
                 "method": row[7],
                 **({"content": row[8]} if include_content else {"content_length": len(row[8] or "")}),
                 "imported_at": row[9], "global_article_id": row[10],
                 "stored_in_sql": True} for row in rows]
    finally:
        connection.close()


def clip_storage_ready(config: dict[str, Any]) -> bool:
    connection = connect(config)
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1 FROM browser_clips LIMIT 1")
        return True
    finally:
        connection.close()


def save_social_posts(posts: list[dict[str, Any]], config: dict[str, Any]) -> int:
    if not posts:
        return 0
    connection = connect(config)
    try:
        with connection.cursor() as cursor:
            for post in posts:
                cursor.execute("""INSERT INTO social_posts
                    (id, platform, source_query, title, content, url, author, created_at,
                     observed_at, language, likes, reposts, replies)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON DUPLICATE KEY UPDATE title=VALUES(title), content=VALUES(content),
                    observed_at=VALUES(observed_at), likes=VALUES(likes),
                    reposts=VALUES(reposts), replies=VALUES(replies)""",
                    (post["id"], post["platform"], post.get("source_query"), post.get("title"),
                     post["content"], post["url"], post.get("author"), post.get("created_at"),
                     post["observed_at"], post.get("language"), post.get("likes", 0),
                     post.get("reposts", 0), post.get("replies", 0)))
        connection.commit()
        return len(posts)
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def save_paper(paper: dict[str, Any], config: dict[str, Any]) -> bool:
    """Return True when a file hash already existed; update editable metadata on re-import."""
    connection = connect(config)
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1 FROM paper_documents WHERE id=%s", (paper["id"],))
            existed = cursor.fetchone() is not None
            cursor.execute("""INSERT INTO paper_documents
                (id,file_hash,filename,title,authors,publication_year,doi,source_url,metadata_json,page_count,text_chars,pages_json,imported_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON DUPLICATE KEY UPDATE title=VALUES(title),authors=VALUES(authors),
                publication_year=VALUES(publication_year),doi=VALUES(doi),
                source_url=COALESCE(NULLIF(VALUES(source_url),''),source_url),
                metadata_json=VALUES(metadata_json),pages_json=VALUES(pages_json),text_chars=VALUES(text_chars)""",
                (paper["id"], paper["file_hash"], paper["filename"], paper["title"],
                 paper["authors"], paper["year"], paper["doi"], paper.get("source_url", ""),
                 json.dumps({**paper.get("metadata", {}), "page_methods": paper.get("page_methods", []),
                             "ocr_warnings": paper.get("ocr_warnings", [])}, ensure_ascii=False), paper["page_count"],
                 paper["text_chars"], json.dumps(paper["pages"], ensure_ascii=False), paper["imported_at"]))
        connection.commit()
        return existed
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def list_papers(config: dict[str, Any]) -> list[dict[str, Any]]:
    connection = connect(config)
    try:
        with connection.cursor() as cursor:
            cursor.execute("""SELECT id,filename,title,authors,publication_year,doi,page_count,text_chars,imported_at,source_url,metadata_json
                FROM paper_documents ORDER BY imported_at DESC""")
            return [{"id": row[0], "filename": row[1], "title": row[2], "authors": row[3] or "",
                     "year": row[4], "doi": row[5] or "", "page_count": row[6],
                     "text_chars": row[7], "imported_at": row[8], "source_url": row[9] or "",
                     "metadata": json.loads(row[10] or "{}"),
                     "url": f"/api/papers/{row[0]}/file"} for row in cursor.fetchall()]
    finally:
        connection.close()


def rag_documents(config: dict[str, Any]) -> list[dict[str, Any]]:
    """Return news, clips, social posts and papers for local retrieval indexing."""
    connection = connect(config)
    try:
        with connection.cursor() as cursor:
            cursor.execute("""SELECT m.id, m.title, m.origin_url, m.source, m.published_at, m.content
                FROM article_master m WHERE m.content IS NOT NULL AND CHAR_LENGTH(m.content) >= 300
                AND EXISTS (SELECT 1 FROM news_articles n WHERE n.global_article_id=m.id
                    AND n.sample_status='初筛合格') ORDER BY m.id""")
            news = [{"kind": "news", "id": row[0], "title": row[1], "url": row[2],
                     "source": row[3] or "", "published_at": row[4] or "",
                     "content": row[5] or ""} for row in cursor.fetchall()]
            cursor.execute("""SELECT id, title, url, source, published_at, content, language
                FROM browser_clips ORDER BY imported_at DESC""")
            clips = [{"kind": "clip", "id": row[0], "title": row[1], "url": row[2],
                      "source": row[3] or "", "published_at": row[4] or "",
                      "content": row[5] or "", "language": row[6] or ""} for row in cursor.fetchall()]
            cursor.execute("""SELECT id, platform, title, content, url, author, created_at,
                language, likes, reposts, replies FROM social_posts
                WHERE NOT (platform='Hacker News' AND content=title)
                ORDER BY observed_at DESC""")
            social = [{"kind": "social", "id": row[0], "title": row[2] or "",
                       "content": row[3] or "", "url": row[4], "source": row[1],
                       "author": row[5] or "", "published_at": row[6] or "",
                       "language": row[7] or "", "likes": row[8], "reposts": row[9],
                       "replies": row[10]} for row in cursor.fetchall()]
            cursor.execute("SHOW TABLES LIKE 'meltwater_mentions'")
            meltwater = []
            if cursor.fetchone():
                cursor.execute("""SELECT id, kind, title, url, source, published_at, excerpt,
                    language, country, sentiment FROM meltwater_mentions
                    WHERE restricted=0 AND kind IN ('news','social') AND CHAR_LENGTH(excerpt)>=60
                    ORDER BY published_at DESC""")
                seen_meltwater_text: set[str] = set()
                for row in cursor.fetchall():
                    fingerprint = content_hash(f"{row[2]}|{row[6]}")
                    if fingerprint in seen_meltwater_text:
                        continue
                    seen_meltwater_text.add(fingerprint)
                    meltwater.append({"kind": row[1], "id": f"meltwater-{row[0]}",
                                      "title": row[2], "url": row[3],
                                      "source": f"融文 · {row[4] or '未知来源'}",
                                      "published_at": row[5] or "",
                                      "content": f"融文匹配片段（非全文）：{row[6]}",
                                      "language": row[7] or "", "country": row[8] or "",
                                      "sentiment": row[9] or ""})
            cursor.execute("""SELECT id,title,authors,publication_year,doi,pages_json,source_url,metadata_json
                FROM paper_documents ORDER BY imported_at DESC""")
            papers = [{"kind": "paper", "id": row[0], "title": row[1],
                       "authors": row[2] or "", "year": row[3], "doi": row[4] or "",
                       "pages": json.loads(row[5]), "url": f"/api/papers/{row[0]}/file",
                       "source": "论文知识库", "published_at": str(row[3] or ""),
                       "source_url": row[6] or "", "journal": json.loads(row[7] or "{}").get("journal", "")}
                      for row in cursor.fetchall()]
        return news + clips + social + meltwater + papers
    finally:
        connection.close()


def sync_local_clips(config: dict[str, Any]) -> int:
    """Copy local browser backups into MySQL after setup or service restart."""
    clip_file = DATA_DIR / "browser-imports.json"
    if not clip_file.exists():
        return 0
    clips = json.loads(clip_file.read_text(encoding="utf-8"))
    if not isinstance(clips, list):
        return 0
    for clip in clips:
        save_clip(clip, config)
    return len(clips)


def save_config(config: dict[str, Any]) -> int:
    test_connection(config)
    initialize_tables(config)
    imported = 0
    if LEGACY_FILE.exists():
        legacy = json.loads(LEGACY_FILE.read_text(encoding="utf-8"))
        if isinstance(legacy, list):
            for run in legacy:
                insert_run(run, config)
                imported += 1
    sync_local_clips(config)
    DATA_DIR.mkdir(exist_ok=True)
    temporary = CONFIG_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
    temporary.replace(CONFIG_FILE)
    return imported


def count_articles(config: dict[str, Any]) -> int:
    connection = connect(config)
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) FROM news_articles")
            return int(cursor.fetchone()[0])
    finally:
        connection.close()


def list_runs(config: dict[str, Any], limit: int = 20, *, run_id: str | None = None,
              include_content: bool = False) -> list[dict[str, Any]]:
    """Read stored assessments and article metadata without exposing saved full text."""
    limit = max(1, min(int(limit), 50))
    connection = connect(config)
    try:
        with connection.cursor() as cursor:
            if run_id:
                cursor.execute("""SELECT id, created_at, task, plan_json, plan_mode, assessment_json
                    FROM research_runs WHERE id=%s""", (run_id,))
            else:
                cursor.execute("""SELECT id, created_at, task, plan_json, plan_mode, assessment_json
                    FROM research_runs ORDER BY created_at DESC, id DESC LIMIT %s""", (limit,))
            rows = cursor.fetchall()
            runs = [
                {
                    "id": row[0], "created_at": row[1], "task": row[2],
                    "plan": json.loads(row[3]), "plan_mode": row[4],
                    "assessment": json.loads(row[5]), "articles": [], "candidates": [],
                    "storage": "MySQL",
                }
                for row in rows
            ]
            if not runs:
                return runs
            by_id = {run["id"]: run for run in runs}
            placeholders = ",".join(["%s"] * len(runs))
            content_column = ", COALESCE(n.content, m.content)" if include_content else ""
            cursor.execute(
                f"""SELECT n.run_id, n.article_id, n.title, n.url, n.source, n.published_at,
                           n.excerpt, n.evidence_level, n.access_note, n.archive_json,
                           n.search_language, n.within_window, n.sample_status, n.content_url, n.genre,
                           n.global_article_id, n.passages_json, n.selection_rank,
                           n.representative_score, n.rerank_score, n.authority,
                           n.search_provenance_json{content_column}
                    FROM news_articles n LEFT JOIN article_master m ON m.id=n.global_article_id
                    WHERE n.run_id IN ({placeholders}) ORDER BY n.run_id, n.article_id""",
                tuple(by_id),
            )
            for row in cursor.fetchall():
                article = {
                    "id": row[1], "title": row[2], "url": row[3],
                    "source": row[4], "published_at": row[5],
                    "excerpt": row[6], "evidence_level": row[7],
                    "access_note": row[8],
                    "archive": json.loads(row[9]) if row[9] else None,
                    "search_language": row[10],
                    "within_window": bool(row[11]) if row[11] is not None else None,
                    "sample_status": row[12],
                    "content_url": row[13] or row[3], "genre": row[14],
                    "global_article_id": row[15],
                    "passages": json.loads(row[16]) if row[16] else [],
                    "selection_rank": row[17], "representative_score": row[18],
                    "rerank_score": row[19], "authority": row[20],
                    "search_queries": (json.loads(row[21]) if row[21] else {}).get("queries", []),
                    "found_languages": (json.loads(row[21]) if row[21] else {}).get("languages", []),
                    "browser_import_id": (json.loads(row[21]) if row[21] else {}).get("browser_import_id"),
                }
                if include_content:
                    article["content"] = row[22] or ""
                run = by_id[row[0]]
                run["candidates"].append(article)
                if run["plan"].get("selection_policy") != "representative_v2" or row[17] is not None:
                    run["articles"].append(article)
            for run in runs:
                if run["plan"].get("selection_policy") == "representative_v2":
                    run["articles"].sort(key=lambda item: item.get("selection_rank") or 9999)
            return runs
    finally:
        connection.close()


def get_run(config: dict[str, Any], run_id: str, *, include_content: bool = False) -> dict[str, Any] | None:
    runs = list_runs(config, run_id=run_id, include_content=include_content)
    return runs[0] if runs else None


def list_evidence_reviews(config: dict[str, Any], run_id: str) -> list[dict[str, str]]:
    connection = connect(config)
    try:
        with connection.cursor() as cursor:
            cursor.execute("""SELECT passage_id,excerpt_hash,status,note,reviewed_at
                FROM evidence_reviews WHERE run_id=%s ORDER BY passage_id""", (run_id,))
            return [{"passage_id": row[0], "excerpt_hash": row[1], "status": row[2],
                     "note": row[3] or "", "reviewed_at": row[4]} for row in cursor.fetchall()]
    finally:
        connection.close()


def save_evidence_review(config: dict[str, Any], run_id: str, passage_id: str,
                         excerpt_hash: str, status: str, note: str) -> dict[str, str]:
    reviewed_at = datetime.now(UTC).isoformat()
    connection = connect(config)
    try:
        with connection.cursor() as cursor:
            cursor.execute("""INSERT INTO evidence_reviews
                (run_id,passage_id,excerpt_hash,status,note,reviewed_at)
                VALUES (%s,%s,%s,%s,%s,%s)
                ON DUPLICATE KEY UPDATE excerpt_hash=VALUES(excerpt_hash),status=VALUES(status),
                note=VALUES(note),reviewed_at=VALUES(reviewed_at)""",
                (run_id, passage_id, excerpt_hash, status, note, reviewed_at))
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return {"passage_id": passage_id, "excerpt_hash": excerpt_hash,
            "status": status, "note": note, "reviewed_at": reviewed_at}
