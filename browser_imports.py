"""User-confirmed article text imported from a local browser tab."""

from __future__ import annotations

import json
import re
import secrets
import threading
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
FILE = DATA_DIR / "browser-imports.json"
TOKEN_FILE = DATA_DIR / "browser-import-token.txt"
LOCK = threading.Lock()
MAX_CONTENT_CHARS = 80_000


def pairing_token() -> str:
    with LOCK:
        DATA_DIR.mkdir(exist_ok=True)
        if TOKEN_FILE.exists():
            return TOKEN_FILE.read_text(encoding="ascii").strip()
        token = secrets.token_urlsafe(24)
        TOKEN_FILE.write_text(token, encoding="ascii")
        return token


def _read() -> list[dict[str, Any]]:
    if not FILE.exists():
        return []
    value = json.loads(FILE.read_text(encoding="utf-8"))
    return value if isinstance(value, list) else []


def list_imports(*, include_content: bool = False) -> list[dict[str, Any]]:
    with LOCK:
        items = _read()
    if include_content:
        return items
    return [{key: value for key, value in item.items() if key != "content"} | {"content_length": len(item.get("content", ""))}
            for item in items]


def selected_imports(ids: list[str]) -> list[dict[str, Any]]:
    wanted = set(ids)
    with LOCK:
        return [item for item in _read() if item.get("id") in wanted]


def save_import(payload: dict[str, Any], canonical_url: str) -> dict[str, Any]:
    url = str(payload.get("url") or "").strip()
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("文章链接必须是 http 或 https 地址。")
    title = re.sub(r"\s+", " ", str(payload.get("title") or "")).strip()[:300]
    source = re.sub(r"\s+", " ", str(payload.get("source") or parsed.hostname or "")).strip()[:120]
    published_at = str(payload.get("published_at") or "").strip()[:10]
    language = str(payload.get("language") or "").strip().lower()[:5]
    content = re.sub(r"[\t\r ]+", " ", str(payload.get("content") or "")).strip()[:MAX_CONTENT_CHARS]
    if not title or len(content) < 300:
        raise ValueError("请确认文章标题和至少 300 字的可见正文。")
    if published_at and not re.fullmatch(r"20\d{2}-\d{2}-\d{2}", published_at):
        raise ValueError("发布日期须为 YYYY-MM-DD。")
    if published_at:
        try:
            date.fromisoformat(published_at)
        except ValueError as error:
            raise ValueError("发布日期不是有效日期。") from error
    method = str(payload.get("extraction_method") or "browser_visible")
    if method not in {"browser_visible", "page_structured_data", "manual_selection", "defuddle", "clean_dom"}:
        method = "browser_visible"
    item = {"id": secrets.token_hex(6), "url": url, "canonical_url": canonical_url,
            "title": title, "source": source, "published_at": published_at or None,
            "language": language or None, "content": content,
            "imported_at": datetime.now(UTC).isoformat(), "method": method}
    with LOCK:
        DATA_DIR.mkdir(exist_ok=True)
        items = _read()
        existing = next((old for old in items if old.get("canonical_url") == canonical_url), None)
        if existing:
            item["id"] = existing["id"]
            items = [old for old in items if old.get("id") != existing["id"]]
        items.insert(0, item)
        temporary = FILE.with_suffix(".tmp")
        temporary.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(FILE)
    return {key: value for key, value in item.items() if key != "content"} | {"content_length": len(content)}
