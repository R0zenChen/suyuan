"""Local bridge to the bundled Meltwater research studio.

The studio owns Chrome login and short-lived authorization. Suyuan only reads its
loopback API and stores downloaded mention metadata and excerpts in MySQL.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

import mysql_store


ROOT = Path(__file__).resolve().parent
DOWNLOAD_DIR = ROOT / "data" / "meltwater"
BASE_URL = f"http://127.0.0.1:{int(os.getenv('SUYUAN_MELTWATER_PORT', '8789'))}"
REDACTED_TITLE = "Content from this publisher requires a premium add-on"


class StudioError(Exception):
    pass


def studio_request(path: str, payload: dict[str, Any] | None = None,
                   *, timeout: int = 15, method: str | None = None) -> dict[str, Any]:
    if not path.startswith("/api/") or "?" in path or "#" in path:
        raise ValueError("融文接口路径无效。")
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    request = Request(BASE_URL + path, data=body,
                      headers={"Content-Type": "application/json; charset=utf-8"} if body is not None else {},
                      method=method or ("POST" if body is not None else "GET"))
    try:
        with urlopen(request, timeout=timeout) as response:
            result = json.load(response)
    except HTTPError as error:
        try:
            message = json.loads(error.read().decode("utf-8")).get("error", "")
        except (ValueError, UnicodeDecodeError):
            message = ""
        raise StudioError(message or f"融文研究台 HTTP {error.code}") from error
    except TimeoutError as error:
        raise StudioError("融文账号连接超时，请确认 Chrome 已打开融文页面。") from error
    except (URLError, OSError) as error:
        raise StudioError("内置融文服务未连接。请确认 Node.js 已安装，并重启溯源服务。") from error
    if not isinstance(result, dict):
        raise StudioError("融文研究台返回格式异常。")
    return result


def validate_search(value: dict[str, Any]) -> dict[str, str]:
    query = str(value.get("query") or "").strip()
    if not query or len(query) > 12000:
        raise ValueError("请填写不超过 12000 字符的融文布尔检索式。")
    try:
        start = date.fromisoformat(str(value.get("from") or ""))
        end = date.fromisoformat(str(value.get("to") or ""))
    except ValueError as error:
        raise ValueError("请填写有效的开始和结束日期。") from error
    if end < start or (end - start).days > 369 or end > datetime.now(ZoneInfo("Asia/Shanghai")).date():
        raise ValueError("融文检索日期需在今天之前，单次最多 370 天。")
    account_key = str(value.get("account_key") or "default")
    if account_key != "pool" and not re.fullmatch(r"default|[a-f0-9]{12}", account_key):
        raise ValueError("融文账号选择无效。")
    return {"query": query, "from": start.isoformat(), "to": end.isoformat(),
            "kind": "all" if value.get("kind") == "all" else "news",
            "accountKey": account_key, "mode": "full"}


def download_export(remote_id: str, expected: int) -> tuple[Path, int]:
    if not re.fullmatch(r"[a-f0-9]{12}", remote_id):
        raise ValueError("融文任务 ID 无效。")
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    target = DOWNLOAD_DIR / f"{remote_id}.jsonl"
    temporary = target.with_suffix(".tmp")
    request = Request(f"{BASE_URL}/api/jobs/{remote_id}/export")
    seen: set[str] = set()
    rows = 0
    try:
        with urlopen(request, timeout=180) as response, temporary.open("wb") as output:
            for line in response:
                if not line.strip():
                    continue
                item = json.loads(line)
                if not isinstance(item, dict):
                    raise StudioError("融文导出包含非对象记录。")
                identifier = mention_id(item)
                if identifier in seen:
                    raise StudioError(f"融文导出存在重复文档 ID：{identifier[:20]}")
                seen.add(identifier)
                output.write(line)
                rows += 1
    except (HTTPError, URLError, TimeoutError, OSError) as error:
        temporary.unlink(missing_ok=True)
        raise StudioError("融文 JSONL 下载失败，请检查研究台任务状态。") from error
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    if rows != expected:
        temporary.unlink(missing_ok=True)
        raise StudioError(f"融文导出校验失败：平台报告 {expected} 条，实际下载 {rows} 条。")
    temporary.replace(target)
    return target, rows


def mention_id(item: dict[str, Any]) -> str:
    identifier = str(item.get("id") or "").strip()
    if not identifier:
        identifier = "|".join(str(item.get(key) or "") for key in ("date", "title", "source", "url"))
    if not identifier.strip("|"):
        raise StudioError("融文记录缺少文档 ID 和可用的备用识别字段。")
    return hashlib.sha256(identifier.encode("utf-8")).hexdigest()


def normalize_mention(item: dict[str, Any], query: str, download_id: str) -> dict[str, Any]:
    title = str(item.get("title") or "").strip()[:2000]
    excerpt = re.sub(r"\s+", " ", str(item.get("excerpt") or "")).strip()[:2000]
    url = str(item.get("url") or "").strip()[:2000]
    kind = str(item.get("type") or "unknown")[:16]
    restricted = REDACTED_TITLE in title or not title or not url.startswith(("https://", "http://"))
    return {"id": mention_id(item), "document_id": str(item.get("id") or "")[:180],
            "kind": kind, "title": title or "标题受限", "url": url,
            "source": str(item.get("source") or "")[:255],
            "published_at": str(item.get("date") or "")[:64], "excerpt": excerpt,
            "language": str(item.get("language") or "")[:16],
            "country": str(item.get("country") or "")[:16],
            "sentiment": str(item.get("sentiment") or "")[:16],
            "account_id": str(item.get("accountId") or "")[:64],
            "query_text": query, "download_id": download_id,
            "restricted": int(restricted)}


def initialize_tables(config: dict[str, Any]) -> None:
    connection = mysql_store.connect(config)
    try:
        with connection.cursor() as cursor:
            cursor.execute("""CREATE TABLE IF NOT EXISTS meltwater_downloads (
                id CHAR(12) PRIMARY KEY, created_at VARCHAR(40) NOT NULL,
                query_text TEXT NOT NULL, date_from CHAR(10) NOT NULL, date_to CHAR(10) NOT NULL,
                content_kind VARCHAR(16) NOT NULL, account_key VARCHAR(32),
                platform_total INT NOT NULL, downloaded_rows INT NOT NULL,
                distinct_rows INT NOT NULL, raw_file VARCHAR(255) NOT NULL,
                KEY idx_mw_download_created (created_at)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""")
            cursor.execute("""CREATE TABLE IF NOT EXISTS meltwater_mentions (
                id CHAR(64) PRIMARY KEY, document_id VARCHAR(180), kind VARCHAR(16) NOT NULL,
                title TEXT NOT NULL, url TEXT NOT NULL, source VARCHAR(255),
                published_at VARCHAR(64), excerpt TEXT, language VARCHAR(16),
                country VARCHAR(16), sentiment VARCHAR(16), account_id VARCHAR(64),
                query_text TEXT, first_download_id CHAR(12) NOT NULL,
                last_download_id CHAR(12) NOT NULL, restricted TINYINT(1) NOT NULL DEFAULT 0,
                imported_at VARCHAR(40) NOT NULL,
                KEY idx_mw_mention_date (published_at), KEY idx_mw_mention_download (last_download_id)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""")
        connection.commit()
    finally:
        connection.close()


def import_download(config: dict[str, Any], file: Path, remote_id: str,
                    search: dict[str, str], expected: int) -> dict[str, int]:
    initialize_tables(config)
    connection = mysql_store.connect(config)
    new_count = 0
    try:
        with connection.cursor() as cursor, file.open("r", encoding="utf-8") as input_file:
            for line in input_file:
                if not line.strip():
                    continue
                item = normalize_mention(json.loads(line), search["query"], remote_id)
                cursor.execute("""INSERT INTO meltwater_mentions
                    (id,document_id,kind,title,url,source,published_at,excerpt,language,country,
                     sentiment,account_id,query_text,first_download_id,last_download_id,restricted,imported_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON DUPLICATE KEY UPDATE last_download_id=VALUES(last_download_id),
                    excerpt=IF(CHAR_LENGTH(VALUES(excerpt))>CHAR_LENGTH(excerpt),VALUES(excerpt),excerpt),
                    imported_at=VALUES(imported_at)""",
                    (item["id"], item["document_id"], item["kind"], item["title"], item["url"],
                     item["source"], item["published_at"], item["excerpt"], item["language"],
                     item["country"], item["sentiment"], item["account_id"], item["query_text"],
                     remote_id, remote_id, item["restricted"], datetime.now(UTC).isoformat()))
                if cursor.rowcount == 1:
                    new_count += 1
            cursor.execute("""INSERT INTO meltwater_downloads
                (id,created_at,query_text,date_from,date_to,content_kind,account_key,
                 platform_total,downloaded_rows,distinct_rows,raw_file)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (remote_id, datetime.now(UTC).isoformat(), search["query"], search["from"],
                 search["to"], search["kind"], search["accountKey"], expected, expected,
                 expected, file.name))
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return {"downloaded": expected, "new": new_count, "existing": expected - new_count}


def list_downloads(config: dict[str, Any]) -> list[dict[str, Any]]:
    initialize_tables(config)
    connection = mysql_store.connect(config)
    try:
        with connection.cursor() as cursor:
            cursor.execute("""SELECT id,created_at,query_text,date_from,date_to,content_kind,
                platform_total,downloaded_rows,account_key FROM meltwater_downloads
                ORDER BY created_at DESC LIMIT 30""")
            return [{"id": row[0], "created_at": row[1], "query": row[2],
                     "from": row[3], "to": row[4], "kind": row[5], "total": row[6],
                     "downloaded": row[7], "account_key": row[8]} for row in cursor.fetchall()]
    finally:
        connection.close()
