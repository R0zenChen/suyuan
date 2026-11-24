"""Local scheduled-topic configuration and run snapshots."""

from __future__ import annotations

import json
import re
import threading
import uuid
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


FILE = Path(__file__).resolve().parent / "data" / "watches.json"
LOCK = threading.RLock()
ZONE = ZoneInfo("Asia/Shanghai")


def summarize_candidates(articles: list[dict[str, Any]]) -> dict[str, Any]:
    seen: set[str] = set()
    dates: dict[str, int] = {}
    languages: dict[str, int] = {}
    sources: dict[str, int] = {}
    for article in articles:
        key = article.get("global_article_id") or article.get("url") or article.get("id")
        if key in seen:
            continue
        seen.add(key)
        day = str(article.get("published_at") or "")[:10]
        if re.fullmatch(r"20\d{2}-\d{2}-\d{2}", day):
            dates[day] = dates.get(day, 0) + 1
        language = article.get("search_language") or "未标注"
        source = article.get("source") or "未标注"
        languages[language] = languages.get(language, 0) + 1
        sources[source] = sources.get(source, 0) + 1
    return {"candidate_count": len(seen), "date_counts": dates,
            "language_counts": languages, "source_counts": sources}


def risk_counts(run: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"heading": point.get("heading", ""), "articles": len(point.get("evidence_ids", []))}
            for point in run.get("assessment", {}).get("risk_points", [])]


def _claim_observation(run: dict[str, Any], point: dict[str, Any]) -> dict[str, Any]:
    by_id = {item.get("id"): item for item in run.get("articles", [])}
    support = [by_id[key] for key in point.get("evidence_ids", []) if key in by_id]
    counter = [by_id[key] for key in point.get("counter_evidence_ids", []) if key in by_id]
    observed = sorted((item for item in support if item.get("published_at")),
                      key=lambda item: str(item["published_at"]))
    first = observed[0] if observed else (support[0] if support else None)
    languages = sorted({item.get("search_language") for item in support if item.get("search_language")})
    original_quotes = []
    for language in languages:
        sample = next((item for item in support if item.get("search_language") == language and item.get("passages")), None)
        if sample:
            original_quotes.append({"language": language, "text": str(sample["passages"][0].get("text") or "")[:280],
                                    "source": sample.get("source"), "url": sample.get("content_url") or sample.get("url")})
    return {"run_id": run["id"], "created_at": run["created_at"], "heading": point.get("heading", ""),
            "status": "observed",
            "risk_score": point.get("risk_score"), "support_ids": point.get("evidence_ids", []),
            "counter_ids": point.get("counter_evidence_ids", []),
            "support_urls": [item.get("url") for item in support if item.get("url")],
            "counter_urls": [item.get("url") for item in counter if item.get("url")],
            "source_count": len({item.get("source") for item in support if item.get("source")}),
            "support_sources": list(dict.fromkeys(item.get("source") for item in support if item.get("source"))),
            "counter_sources": list(dict.fromkeys(item.get("source") for item in counter if item.get("source"))),
            "languages": languages, "original_quotes": original_quotes,
            "translation_note": point.get("translation_note", ""),
            "score_reason": point.get("score_reason", ""),
            "first_observed": ({"date": first.get("published_at"), "source": first.get("source"),
                                 "url": first.get("url")} if first else None)}


def reconcile_claims(watch: dict[str, Any], run: dict[str, Any],
                     prior: dict[str, Any] | None = None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Give each risk claim a stable local ID; low-confidence matches remain separate."""
    claims = json.loads(json.dumps(watch.get("claims") or [], ensure_ascii=False))
    if not claims and prior and prior.get("id") != run.get("id"):
        claims, _ = reconcile_claims({"id": watch["id"]}, prior)
    used: set[str] = set()
    changes = []
    for point in run.get("assessment", {}).get("risk_points", []):
        heading = str(point.get("heading") or "").strip()
        if not heading:
            continue
        options = []
        for claim in claims:
            if claim["id"] in used:
                continue
            previous = next((entry for entry in reversed(claim["history"])
                             if entry.get("status") != "not_repeated"), claim["history"][-1])
            similarity = SequenceMatcher(None, heading, previous["heading"]).ratio()
            left, right = set(previous.get("support_urls") or []), {
                item.get("url") for item in run.get("articles", []) if item.get("id") in point.get("evidence_ids", [])}
            overlap = len(left & right) / max(1, len(left | right))
            options.append((similarity * 0.8 + overlap * 0.2, claim))
        best_score, best = max(options, key=lambda item: item[0]) if options else (0, None)
        if best_score < 0.66:
            best = None
        claim_id = best["id"] if best else uuid.uuid5(uuid.NAMESPACE_URL, f"{watch['id']}:{heading}").hex[:12]
        point["claim_id"] = claim_id
        used.add(claim_id)
        observation = _claim_observation(run, point)
        if best:
            previous = next((entry for entry in reversed(best["history"])
                             if entry.get("status") != "not_repeated"), best["history"][-1])
            if previous["run_id"] != run["id"]:
                best["history"].append(observation)
            best["label"] = heading
            old_urls = set(previous.get("support_urls") or [])
            new_urls = set(observation["support_urls"]) - old_urls
            old_score, new_score = previous.get("risk_score"), observation.get("risk_score")
            delta = new_score - old_score if isinstance(old_score, int) and isinstance(new_score, int) else None
            change = {"claim_id": claim_id, "label": heading, "kind": "updated", "new_sources": len(new_urls),
                      "risk_delta": delta, "match_score": round(best_score, 2)}
        else:
            claims.append({"id": claim_id, "label": heading, "first_seen": run["created_at"],
                           "history": [observation], "review_note": ""})
            change = {"claim_id": claim_id, "label": heading, "kind": "new", "new_sources": len(observation["support_urls"]),
                      "risk_delta": None, "match_score": None}
        changes.append(change)
    for claim in claims:
        if claim["id"] in used or claim["history"][-1]["run_id"] == run["id"]:
            continue
        previous = claim["history"][-1]
        absent = {**previous, "run_id": run["id"], "created_at": run["created_at"],
                  "status": "not_repeated", "support_ids": [], "counter_ids": [],
                  "support_urls": [], "counter_urls": [], "risk_score": None,
                  "source_count": 0, "support_sources": [], "counter_sources": [],
                  "original_quotes": [], "translation_note": "", "score_reason": ""}
        claim["history"].append(absent)
        changes.append({"claim_id": claim["id"], "label": claim["label"], "kind": "not_repeated",
                        "new_sources": 0, "risk_delta": None, "match_score": None})
    return claims, changes


def now_local() -> datetime:
    return datetime.now(ZONE)


def next_run_at(daily_time: str, now: datetime | None = None) -> str:
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", daily_time):
        raise ValueError("每日运行时间须为 HH:MM。")
    current = now or now_local()
    hour, minute = map(int, daily_time.split(":"))
    scheduled = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if scheduled <= current:
        scheduled += timedelta(days=1)
    return scheduled.isoformat()


def _read() -> list[dict[str, Any]]:
    if not FILE.exists():
        return []
    value = json.loads(FILE.read_text(encoding="utf-8"))
    return value if isinstance(value, list) else []


def _write(watches: list[dict[str, Any]]) -> None:
    FILE.parent.mkdir(exist_ok=True)
    temporary = FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(watches, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(FILE)


def list_watches() -> list[dict[str, Any]]:
    with LOCK:
        return _read()


def get_watch(watch_id: str) -> dict[str, Any] | None:
    return next((item for item in list_watches() if item["id"] == watch_id), None)


def save_watch(run: dict[str, Any], daily_time: str) -> dict[str, Any]:
    next_at = next_run_at(daily_time)
    with LOCK:
        watches = _read()
        existing_id = run.get("plan", {}).get("watch_id")
        watch = next((item for item in watches if item.get("seed_run_id") == run["id"] or
                      (existing_id and item["id"] == existing_id)), None)
        if watch:
            watch.update({"daily_time": daily_time, "next_run_at": next_at, "enabled": True})
            if not watch.get("claims"):
                watch["claims"], _ = reconcile_claims(watch, run)
        else:
            current_standard = run.get("plan", {}).get("selection_policy") == "representative_v2"
            baseline = [{"run_id": run["id"], "created_at": run.get("created_at"),
                         "kind": "baseline", "new_count": 0,
                         "selected_count": len(run.get("articles", [])), "new_risk_count": 0,
                         "risk_count": len(run.get("assessment", {}).get("risk_points", [])),
                         "risk_point_counts": risk_counts(run),
                         **summarize_candidates(run.get("candidates", run.get("articles", [])))}] if current_standard else []
            watch = {
                "id": uuid.uuid4().hex[:12], "seed_run_id": run["id"],
                "task": run["task"], "topic": run.get("plan", {}).get("topic") or run["task"],
                "languages": run.get("plan", {}).get("languages", []),
                "daily_time": daily_time, "next_run_at": next_at, "enabled": True,
                "last_run_id": run["id"] if current_standard else None,
                "last_checked_date": run.get("plan", {}).get("end_date") if current_standard else None,
                "history": baseline, "last_alert": None,
                "seen_urls": [article["url"] for article in run.get("candidates", run.get("articles", [])) if article.get("url")],
            }
            watch["claims"], _ = reconcile_claims(watch, run)
            watches.insert(0, watch)
        _write(watches)
        return watch


def set_enabled(watch_id: str, enabled: bool) -> dict[str, Any] | None:
    with LOCK:
        watches = _read()
        for watch in watches:
            if watch["id"] == watch_id:
                watch["enabled"] = enabled
                if enabled:
                    watch["next_run_at"] = next_run_at(watch["daily_time"])
                _write(watches)
                return watch
    return None


def due_watch(now: datetime | None = None) -> dict[str, Any] | None:
    current = now or now_local()
    with LOCK:
        for watch in _read():
            if watch.get("enabled") and datetime.fromisoformat(watch["next_run_at"]) <= current:
                return watch
    return None


def claim_due(watch_id: str, now: datetime | None = None) -> dict[str, Any] | None:
    current = now or now_local()
    with LOCK:
        watches = _read()
        for watch in watches:
            if watch["id"] == watch_id and watch.get("enabled") and datetime.fromisoformat(watch["next_run_at"]) <= current:
                watch["next_run_at"] = next_run_at(watch["daily_time"], current + timedelta(minutes=1))
                _write(watches)
                return watch
    return None


def record_result(watch_id: str, run: dict[str, Any], prior: dict[str, Any] | None = None) -> None:
    with LOCK:
        watches = _read()
        for watch in watches:
            if watch["id"] != watch_id:
                continue
            plan = run.get("plan", {})
            new_count = int(plan.get("new_count", len(run.get("articles", []))))
            new_risk_count = int(plan.get("new_risk_count", 0))
            new_candidate_ids = set(plan.get("new_candidate_ids") or [])
            new_candidates = [article for article in run.get("candidates", [])
                              if "new_candidate_ids" not in plan or article.get("id") in new_candidate_ids]
            is_baseline = not watch.get("history")
            snapshot = {
                "run_id": run["id"], "created_at": run["created_at"],
                "kind": "baseline" if is_baseline else "incremental",
                "new_count": new_count, "selected_count": len(run.get("articles", [])),
                "new_risk_count": new_risk_count,
                "risk_count": len(run.get("assessment", {}).get("risk_points", [])),
                "risk_point_counts": risk_counts(run),
                **summarize_candidates(new_candidates),
            }
            watch["history"] = (watch.get("history") or [])[-89:] + [snapshot]
            watch["languages"] = list(plan.get("languages") or watch.get("languages") or [])
            watch["claims"], _ = reconcile_claims(watch, run, prior)
            watch["last_run_id"] = run["id"]
            watch["last_checked_date"] = plan.get("end_date")
            watch["seen_urls"] = list(dict.fromkeys((watch.get("seen_urls") or []) +
                [article["url"] for article in run.get("candidates", []) if article.get("url")]))
            if not is_baseline and (new_count >= 5 or new_risk_count >= 1):
                watch["last_alert"] = {
                    "created_at": run["created_at"], "run_id": run["id"],
                    "message": f"新增 {new_count} 篇代表材料、{new_risk_count} 个候选新风险点，请复核。",
                }
            _write(watches)
            return
