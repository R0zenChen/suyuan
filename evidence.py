"""Traceable excerpts and transparent evidence-strength scoring."""

from __future__ import annotations

import re
from urllib.parse import urlparse
from typing import Any


def passages_for_article(article: dict[str, Any], count: int = 3, width: int = 520) -> list[dict[str, Any]]:
    """Return exact, position-spread excerpts from the fetched full text."""
    content = str(article.get("content") or "")
    if not content:
        return []
    max_start = max(0, len(content) - width)
    starts = [round(max_start * index / max(1, count - 1)) for index in range(count)]
    passages: list[dict[str, Any]] = []
    for start in dict.fromkeys(starts):
        raw = content[start:start + width]
        text = raw.strip()
        if len(text) < 80:
            continue
        actual_start = start + len(raw) - len(raw.lstrip())
        passages.append({
            "id": f"{article['id']}-P{len(passages) + 1}",
            "text": text,
            "start": actual_start,
        })
    return passages


def valid_passage_ids(raw_ids: Any, article_ids: list[str], evidence: list[dict[str, Any]]) -> list[str]:
    if not isinstance(raw_ids, list):
        return []
    allowed = {
        passage["id"]: item["id"]
        for item in evidence
        for passage in item.get("passages", [])
        if isinstance(passage, dict) and isinstance(passage.get("id"), str)
    }
    return list(dict.fromkeys(
        value for value in raw_ids
        if isinstance(value, str) and allowed.get(value) in article_ids
    ))


def evidence_strength(article_ids: list[str], passage_ids: list[str], evidence: list[dict[str, Any]],
                      priority_domains: tuple[str, ...]) -> dict[str, Any]:
    """This scores evidence coverage, not real-world probability or reach."""
    by_id = {item["id"]: item for item in evidence}
    cited = [by_id[item_id] for item_id in article_ids if item_id in by_id]
    domains = {(urlparse(item.get("url") or "").hostname or "").lower() for item in cited}
    domains.discard("")
    quoted_articles = {passage_id.split("-P", 1)[0] for passage_id in passage_ids}
    authority = sum(1 for item in cited if any(
        (urlparse(item.get("url") or "").hostname or "").lower() == domain or
        (urlparse(item.get("url") or "").hostname or "").lower().endswith(f".{domain}")
        for domain in priority_domains
    ))
    dated = sum(1 for item in cited if re.search(r"20\d{2}-\d{2}-\d{2}", str(item.get("date") or "")))
    total = max(1, len(cited))
    components = {
        "独立报道": min(25, len(cited) * 5),
        "来源多样": min(25, len(domains) * 5),
        "原文段落": round(30 * len(quoted_articles) / total),
        "日期与权威": round(10 * dated / total) + round(10 * authority / total),
    }
    return {"score": min(100, sum(components.values())), "components": components,
            "article_count": len(cited), "source_count": len(domains),
            "quoted_article_count": len(quoted_articles)}
