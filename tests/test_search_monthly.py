import json
import hashlib
import re
import threading
import unittest
from datetime import date
from unittest.mock import patch

import app


class SearchPolicyTests(unittest.TestCase):
    def test_nine_languages_get_two_dated_queries_each(self):
        planned = {language: [f"{language} topic analysis", f"{language} topic editorial"]
                   for language in app.DEFAULT_LANGUAGES}
        client = app.DeepSeekClient.__new__(app.DeepSeekClient)
        client.model = "test"
        response = {"choices": [{"message": {"content": json.dumps({"topic": "议题", "queries_by_language": planned})}}]}
        with patch.object(client, "post", return_value=response):
            plan, mode = client.plan_research("关于某议题的国际报道", list(app.DEFAULT_LANGUAGES), date(2026, 10, 1), date(2026, 10, 31))
        self.assertEqual(len(plan["query_specs"]), 18)
        self.assertEqual([item["language"] for item in plan["query_specs"]][::2], list(app.DEFAULT_LANGUAGES))
        self.assertTrue({"ru", "ar", "pt"}.issubset(plan["languages"]))
        self.assertNotIn("vi", plan["languages"])
        self.assertTrue(all("after:2026-10-01" in item["query"] and "before:2026-11-01" in item["query"] for item in plan["query_specs"]))
        self.assertEqual(mode, "多语种检索 · 模型规划")
        self.assertEqual(plan["target_count"], 30)
        self.assertEqual(plan["required_risk_points"], 4)

    def test_explicit_languages_replace_defaults_and_long_window_is_rejected(self):
        self.assertEqual(app.search_languages("仅检索日语和韩语报道"), ["ko", "ja"])
        self.assertEqual(app.search_languages("仅检索俄语和阿拉伯语报道"), ["ru", "ar"])
        self.assertEqual(app.search_languages("任意议题", "俄文,阿语,葡文"), ["ru", "ar", "pt"])
        with self.assertRaises(app.ApiError):
            app.search_languages("任意议题", "vi")
        self.assertEqual(app.search_languages("only russian and arabic reports"), ["ru", "ar"])
        self.assertEqual(app.search_languages("任意议题", "日语,韩语"), ["ja", "ko"])
        with self.assertRaises(app.ApiError):
            app.search_window("任意议题", "2026-01-01", "2026-03-01")

    def test_model_failure_still_runs_distinct_queries_and_marks_fallback(self):
        client = app.DeepSeekClient.__new__(app.DeepSeekClient)
        client.model = "test"
        with patch.object(client, "post", side_effect=app.ApiError(502, "offline")):
            plan, mode = client.plan_research("国际媒体对某项政策的报道", ["en", "ja"], date(2026, 10, 1), date(2026, 10, 31))
        self.assertEqual(mode, "多语种检索 · 部分降级")
        self.assertEqual(plan["query_fallback_languages"], ["en", "ja"])
        self.assertEqual(len({item["query"] for item in plan["query_specs"]}), 4)

    def test_search_retries_temporary_rate_limit(self):
        client = app.DeepSeekClient.__new__(app.DeepSeekClient)
        with patch.object(client, "post", side_effect=[app.ApiError(429, "rate limit"), {"results": [{"url": "https://example.com/a"}]}]) as posted:
            with patch.object(app.time, "sleep"):
                result = client.search("topic lang:en")
        self.assertEqual(len(result), 1)
        self.assertEqual(posted.call_count, 2)
        self.assertEqual(posted.call_args.args[1]["count"], 50)

    def test_collection_tracks_30_initial_passes_and_language_queries(self):
        class FakeClient:
            calls = 0

            def search(self, query):
                self.calls += 1
                language = re.search(r"lang:(\w+)", query).group(1)
                return [{"url": f"https://example.com/{language}/{self.calls}/{number}",
                         "title": f"{language} analysis {number}", "date": "2026-10-15", "site_name": "Example"}
                        for number in range(4)]

            def fetch(self, url):
                token = hashlib.sha256(url.encode()).hexdigest()
                return {"title": f"Long analysis {url}", "content": (token + " substantive independent argument ") * 100}

            def classify_articles(self, articles, accepted):
                return {item["id"]: {"accepted": True, "genre": "专题分析", "duplicate_of": None}
                        for item in articles}

        plan = {"languages": list(app.DEFAULT_LANGUAGES), "start_date": "2026-10-01", "end_date": "2026-10-31",
                "query_specs": [{"language": language, "query": f"topic lang:{language}"}
                                for language in app.DEFAULT_LANGUAGES for _ in range(2)]}
        articles = app.collect_research_articles(FakeClient(), plan, lambda _message: None)
        self.assertGreaterEqual(plan["coverage"]["initial_pass"], 30)
        self.assertEqual(plan["coverage"]["gap"], 0)
        self.assertEqual(len(plan["coverage"]["languages"]), 9)
        self.assertTrue(all(item["queries"] == 2 for item in plan["coverage"]["languages"]))
        self.assertTrue(all(article["sample_status"] == "初筛合格" for article in articles))
        self.assertLessEqual(len(articles), app.MAX_FETCH_CANDIDATES)

    def test_search_groups_run_in_parallel_without_losing_language_counts(self):
        gate = threading.Barrier(4, timeout=2)

        class FakeClient:
            def search(self, _query):
                gate.wait()
                return []

        languages = ["en", "ru", "ar", "pt"]
        plan = {"languages": languages, "start_date": "2026-10-01", "end_date": "2026-10-31",
                "query_specs": [{"language": language, "query": f"topic lang:{language}"}
                                for language in languages]}
        updates = []
        articles = app.collect_research_articles(FakeClient(), plan, updates.append)
        self.assertEqual(articles, [])
        self.assertEqual(plan["coverage"]["search_calls"], 4)
        self.assertEqual([row["language"] for row in plan["coverage"]["languages"]], languages)
        self.assertEqual([update["done"] for update in updates if update.get("stage") == "search"], [0, 1, 2, 3, 4])

    def test_reposts_and_wire_dispatches_do_not_enter_selected_articles(self):
        class FakeClient:
            def search(self, _query):
                return [
                    {"url": "https://newspaper.example/reprint", "title": "Original analysis", "date": "2026-10-15"},
                    {"url": "https://reuters.com/world/analysis", "title": "Original analysis", "date": "2026-10-15"},
                    {"url": "https://wire.example/brief", "title": "Event brief", "date": "2026-10-15"},
                ]

            def fetch(self, url):
                body = ("event brief notice " if "brief" in url else "independent analytical argument ") * 170
                return {"title": "Original analysis" if "brief" not in url else "Event brief", "content": body}

            def classify_articles(self, articles, _accepted):
                return {item["id"]: {"accepted": "brief" not in item["url"],
                                     "genre": "专题分析" if "brief" not in item["url"] else "通稿",
                                     "duplicate_of": None}
                        for item in articles}

        plan = {"languages": ["en"], "start_date": "2026-10-01", "end_date": "2026-10-31",
                "query_specs": [{"language": "en", "query": "topic lang:en"}]}
        articles = app.collect_research_articles(FakeClient(), plan, lambda _message: None)
        self.assertEqual(len(articles), 1)
        self.assertEqual(articles[0]["url"], "https://reuters.com/world/analysis")
        self.assertEqual(plan["coverage"]["duplicate_count"], 1)
        self.assertEqual(plan["coverage"]["excluded_count"], 1)

    def test_attributed_wire_copy_is_excluded_before_model_classification(self):
        article = {"url": "https://example.org/news/story", "title": "Policy outlook",
                   "content": "By Reuters. A long analysis follows. " * 150}
        self.assertTrue(app.obvious_syndicated_copy(article))
        self.assertFalse(app.obvious_syndicated_copy({**article, "url": "https://www.reuters.com/world/story"}))

    def test_risk_points_require_four_distinct_articles_each_but_may_reuse_across_points(self):
        evidence = [{"id": f"A{index}"} for index in range(1, 9)]
        points = [
            {"heading": f"风险点{index}", "analysis": "依据", "evidence_ids": ["A1", "A2", "A3", "A4", "A4"]}
            for index in range(1, 5)
        ]
        points.append({"heading": "证据不足", "analysis": "不足", "evidence_ids": ["A1", "A2", "X9"]})
        result = app.normalize_assessment({"risk_points": points, "risk_level": "中"}, evidence)
        self.assertEqual(len(result["risk_points"]), 4)
        self.assertTrue(all(len(point["evidence_ids"]) == 4 for point in result["risk_points"]))

    def test_every_run_uses_full_search_and_requires_materials_and_risk_points(self):
        task = "检索国际媒体对人工智能监管政策的深度报道"
        with patch.object(app, "DeepSeekClient") as crawler_class, \
             patch.object(app, "AnalysisClient") as analysis_class, \
             patch.object(app, "collect_research_articles") as collect, \
             patch.object(app, "record_run"), \
             patch.object(app.mysql_store, "load_config", return_value=None):
            crawler = crawler_class.return_value
            crawler.configured = True
            analyst = analysis_class.return_value
            analyst.configured = True
            analyst.source = "自定义分析接口"
            analyst.format = "anthropic"
            analyst.model = "demo-model"
            analyst.assess.return_value = {
                "risk_points": [{"heading": f"风险{index}", "evidence_ids": ["A1", "A2", "A3", "A4"]} for index in range(4)],
                "limitations": [],
            }
            for count, expected in ((30, True), (12, False)):
                plan = {"coverage": {"initial_pass": count, "gap": max(0, 30 - count)}, "languages": ["en"], "queries": []}
                analyst.plan_research.return_value = (plan, "多语种检索")
                collect.return_value = [{"id": f"A{index}", "sample_status": "初筛合格", "content": "evidence"} for index in range(1, count + 1)]
                result = app.execute_research({"task": task, "profile": "quick"}, lambda _message: None)
                self.assertEqual(result["plan"]["coverage"]["structure_met"], expected)
                self.assertEqual(result["plan"]["analysis_provider"]["model"], "demo-model")
                self.assertIs(collect.call_args.kwargs["analysis_client"], analyst)
                self.assertNotIn("content", result["articles"][0])
            self.assertEqual(analyst.plan_research.call_count, 2)
            crawler.plan_research.assert_not_called()


if __name__ == "__main__":
    unittest.main()
