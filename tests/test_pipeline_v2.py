import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import app
import browser_imports
import evidence
import watch_store


class RetrievalAndEvidenceTests(unittest.TestCase):
    def test_browser_import_replaces_fetch_for_matching_search_result(self):
        class Searcher:
            def search(self, _query):
                return [{"url": "https://example.org/analysis?utm_source=test", "title": "Analysis"}]

            def fetch(self, _url):
                raise AssertionError("An imported article must not be fetched again")

            def classify_articles(self, articles, _accepted):
                return {item["id"]: {"accepted": True, "genre": "专题分析"} for item in articles}

        imported = {"id": "a" * 12, "url": "https://example.org/analysis",
                    "title": "Long analysis", "source": "Example", "published_at": "2026-10-01",
                    "language": "en", "content": "Independent research and argument. " * 150}
        plan = {"languages": ["en"], "start_date": "2026-10-01", "end_date": "2026-10-02",
                "query_specs": [{"language": "en", "query": "topic lang:en"}]}
        selected = app.collect_research_articles(Searcher(), plan, lambda _item: None,
                                                 manual_imports=[imported])
        self.assertEqual(len(selected), 1)
        self.assertEqual(len(plan["candidate_articles"]), 1)
        self.assertEqual(selected[0]["evidence_level"], "浏览器导入正文")
        self.assertEqual(selected[0]["browser_import_id"], imported["id"])
        self.assertEqual(plan["coverage"]["candidate_count"], 1)

    def test_browser_import_store_upserts_without_exposing_content_in_list(self):
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(browser_imports, "DATA_DIR", Path(folder)), \
             patch.object(browser_imports, "FILE", Path(folder) / "imports.json"), \
             patch.object(browser_imports, "TOKEN_FILE", Path(folder) / "token.txt"):
            payload = {"url": "https://example.org/analysis", "title": "Analysis",
                       "source": "Example", "published_at": "2026-10-01", "language": "en",
                       "content": "Useful analysis. " * 100}
            first = browser_imports.save_import(payload, payload["url"])
            second = browser_imports.save_import({**payload, "title": "Revised analysis"}, payload["url"])
            self.assertEqual(first["id"], second["id"])
            self.assertEqual(len(browser_imports.list_imports()), 1)
            self.assertNotIn("content", browser_imports.list_imports()[0])
            self.assertIn("Useful analysis", browser_imports.selected_imports([first["id"]])[0]["content"])
            with self.assertRaises(ValueError):
                browser_imports.save_import({**payload, "published_at": "2026-02-30"}, payload["url"])
            undated = browser_imports.save_import({**payload, "published_at": "", "extraction_method": "defuddle"}, payload["url"])
            self.assertIsNone(undated["published_at"])
            self.assertEqual(browser_imports.selected_imports([undated["id"]])[0]["method"], "defuddle")

    def test_full_search_split_when_fifty_results_are_returned(self):
        class Searcher:
            def __init__(self):
                self.queries = []

            def search(self, query):
                self.queries.append(query)
                return [{"url": f"https://example.org/{i}"} for i in range(50 if "after:2026-10-01 before:2026-10-05" in query else 3)]

        client = Searcher()
        results, stats = app.search_by_time_segments(client, "topic lang:en after:2026-10-01 before:2026-10-05",
                                                     date(2026, 10, 1), date(2026, 10, 4))
        self.assertEqual(stats["search_calls"], 3)
        self.assertEqual(stats["saturated_days"], [])
        self.assertEqual(len(results), 56)
        self.assertTrue(all("after:" in query and "before:" in query for query in client.queries))

    def test_single_saturated_day_is_reported(self):
        class Searcher:
            def search(self, _query):
                return [{}] * 50

        _results, stats = app.search_by_time_segments(Searcher(), "topic lang:en", date(2026, 10, 1), date(2026, 10, 1))
        self.assertEqual(stats["saturated_days"], ["2026-10-01"])

    def test_every_fetched_candidate_is_retained_even_if_not_selected(self):
        class Searcher:
            def search(self, _query):
                return [{"url": "https://example.org/analysis", "title": "Analysis", "date": "2026-10-01"},
                        {"url": "https://example.org/brief", "title": "Brief", "date": "2026-10-01"}]

            def fetch(self, url):
                return {"content": ("independent argument " if "analysis" in url else "short news ") * 200}

            def classify_articles(self, articles, _accepted):
                return {item["id"]: {"accepted": True, "genre": "专题分析"} for item in articles}

        plan = {"languages": ["en"], "start_date": "2026-10-01", "end_date": "2026-10-02",
                "query_specs": [{"language": "en", "query": "topic lang:en"}]}
        selected = app.collect_research_articles(Searcher(), plan, lambda _item: None)
        self.assertEqual(len(selected), 1)
        self.assertEqual(len(plan["candidate_articles"]), 2)
        self.assertEqual({item["sample_status"] for item in plan["candidate_articles"]}, {"初筛合格", "非长篇"})

    def test_passages_are_exact_and_risks_require_real_passage_ids(self):
        article = {"id": "A1", "content": "Alpha research. " * 200}
        passages = evidence.passages_for_article(article)
        self.assertEqual(len(passages), 3)
        self.assertTrue(all(article["content"][item["start"]:item["start"] + len(item["text"])] == item["text"] for item in passages))
        sources = [{"id": f"A{i}", "url": f"https://source{i}.org/a", "date": "2026-10-01",
                    "passages": [{"id": f"A{i}-P1", "text": "Grounded quote"}]} for i in range(1, 5)]
        point = {"heading": "风险", "analysis": "依据", "evidence_ids": [f"A{i}" for i in range(1, 5)],
                 "evidence_passage_ids": ["A1-P1", "A2-P1", "A3-P1", "X9-P1"],
                 "likelihood": 3, "impact": 4}
        self.assertEqual(app.normalize_assessment({"risk_points": [point]}, sources)["risk_points"], [])
        point["evidence_passage_ids"][-1] = "A4-P1"
        checked = app.normalize_assessment({"risk_points": [point]}, sources)["risk_points"][0]
        self.assertEqual(checked["risk_score"], 12)
        self.assertEqual(checked["evidence_strength"]["quoted_article_count"], 4)


class WatchStoreTests(unittest.TestCase):
    def test_daily_schedule_and_result_snapshot(self):
        shanghai = ZoneInfo("Asia/Shanghai")
        now = datetime(2026, 11, 7, 10, 0, tzinfo=shanghai)
        self.assertEqual(watch_store.next_run_at("09:00", now), "2026-11-08T09:00:00+08:00")
        with tempfile.TemporaryDirectory() as folder, patch.object(watch_store, "FILE", Path(folder) / "watches.json"):
            run = {"id": "a" * 12, "task": "检索某议题的多语种深度报道", "plan": {
                "topic": "某议题", "languages": ["en"], "end_date": "2026-11-07", "selection_policy": "representative_v2"},
                "articles": [], "candidates": [{"url": "https://example.org/a"}]}
            watch = watch_store.save_watch(run, "09:00")
            self.assertEqual(watch["last_run_id"], run["id"])
            self.assertTrue(watch_store.get_watch(watch["id"])["enabled"])
            result = {"id": "b" * 12, "created_at": now.isoformat(),
                      "plan": {"end_date": "2026-11-07", "new_count": 6, "new_risk_count": 1},
                      "articles": [{}] * 30, "candidates": [{"url": "https://example.org/b"}],
                      "assessment": {"risk_points": [{}] * 4}}
            watch_store.record_result(watch["id"], result)
            updated = watch_store.get_watch(watch["id"])
            self.assertEqual(updated["history"][-1]["new_count"], 6)
            self.assertEqual(len(updated["seen_urls"]), 2)
            self.assertIn("新增 6 篇", updated["last_alert"]["message"])


if __name__ == "__main__":
    unittest.main()
