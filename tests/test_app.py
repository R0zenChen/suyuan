import json
import tempfile
import threading
import time
import unittest
from http.client import HTTPConnection
from pathlib import Path
from unittest.mock import patch

import app
import analysis_store
import browser_imports
import watch_store


class SuyuanPolicyTests(unittest.TestCase):
    @patch("app.launch_paper_import_job", return_value="a" * 12)
    @patch("app.mysql_store.load_config", return_value={"database": "suyuan"})
    def test_extension_manual_pdf_upload_queues_import(self, _database_config, launch_job):
        origin = "chrome-extension://" + "a" * 32
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(browser_imports, "DATA_DIR", Path(folder)), \
             patch.object(browser_imports, "TOKEN_FILE", Path(folder) / "token.txt"):
            token = browser_imports.pairing_token()
            payload = {"filename": "research.pdf", "pdf_base64": "JVBERi0xLjQ=",
                       "metadata": {"title": "Research paper"}, "source_url": ""}
            connection = HTTPConnection("127.0.0.1", self.port, timeout=2)
            try:
                connection.request("POST", "/api/browser/paper", json.dumps(payload).encode(), {
                    "Content-Type": "application/json", "Origin": origin,
                    "X-Suyuan-Import-Key": token})
            except PermissionError as error:
                self.skipTest(f"当前受限执行环境禁止本机回环请求：{error}")
            response = connection.getresponse()
            result = json.loads(response.read().decode("utf-8"))
            connection.close()
            self.assertEqual(response.status, 202)
            self.assertEqual(result["job_id"], "a" * 12)
            self.assertEqual(launch_job.call_args.args[0]["filename"], "research.pdf")

    @patch("app.mysql_store.load_config", return_value=None)
    def test_extension_import_requires_pairing_and_lists_metadata(self, _database_config):
        origin = "chrome-extension://" + "a" * 32
        article = {"url": "https://example.org/opinion", "title": "Opinion",
                   "source": "Example", "published_at": "2026-10-01", "language": "en",
                   "content": "Detailed analysis and independent viewpoint. " * 100}
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(browser_imports, "DATA_DIR", Path(folder)), \
             patch.object(browser_imports, "FILE", Path(folder) / "imports.json"), \
             patch.object(browser_imports, "TOKEN_FILE", Path(folder) / "token.txt"):
            def request(method, path, body=None, headers=None):
                connection = HTTPConnection("127.0.0.1", self.port, timeout=2)
                try:
                    connection.request(method, path, body, headers or {})
                    response = connection.getresponse()
                    return response.status, dict(response.getheaders()), response.read()
                finally:
                    connection.close()
            try:
                status, _, body = request("GET", "/api/browser/imports")
            except PermissionError as error:
                self.skipTest(f"当前受限执行环境禁止本机回环请求：{error}")
            listing = json.loads(body.decode("utf-8"))
            self.assertEqual(status, 200)
            token = listing["pairing_token"]
            headers = {"Content-Type": "application/json", "Origin": origin,
                       "X-Suyuan-Import-Key": "incorrect"}
            status, response_headers, _ = request("OPTIONS", "/api/browser/test", headers={"Origin": origin})
            self.assertEqual(status, 204)
            self.assertEqual(response_headers.get("Access-Control-Allow-Origin"), origin)
            status, _, _ = request("POST", "/api/browser/test", b"{}", headers)
            self.assertEqual(status, 403)
            headers["X-Suyuan-Import-Key"] = token
            status, response_headers, body = request("POST", "/api/browser/test", b"{}", headers)
            self.assertEqual(status, 200)
            self.assertEqual(response_headers.get("Access-Control-Allow-Origin"), origin)
            self.assertEqual(json.loads(body.decode("utf-8"))["service"], "溯源")
            self.assertEqual(browser_imports.list_imports(), [])
            headers["X-Suyuan-Import-Key"] = "incorrect"
            status, _, _ = request("POST", "/api/browser/import", json.dumps(article).encode("utf-8"), headers)
            self.assertEqual(status, 403)
            headers["X-Suyuan-Import-Key"] = token
            status, response_headers, body = request("POST", "/api/browser/import", json.dumps(article).encode("utf-8"), headers)
            imported = json.loads(body.decode("utf-8"))
            self.assertEqual(status, 200)
            self.assertEqual(response_headers.get("Access-Control-Allow-Origin"), origin)
            self.assertNotIn("content", imported["article"])
            self.assertFalse(imported["article"]["stored_in_sql"])
            _, _, body = request("GET", "/api/browser/imports")
            listing = json.loads(body.decode("utf-8"))
            self.assertEqual(len(listing["imports"]), 1)
            self.assertNotIn("content", listing["imports"][0])

    @patch("app.mysql_store.list_clips")
    @patch("app.mysql_store.load_config", return_value={"database": "suyuan"})
    @patch("app.browser_imports.list_imports", return_value=[])
    def test_browser_library_recovers_clips_from_sql_after_local_backup_is_gone(self, _local, _config, sql_clips):
        sql_clips.return_value = [{"id": "a" * 12, "url": "https://example.org/a",
                                   "canonical_url": "https://example.org/a", "title": "Analysis",
                                   "content": "Long analysis" * 30, "imported_at": "2026-11-07T00:00:00Z",
                                   "stored_in_sql": True}]
        items, warning = app.browser_library()
        self.assertIsNone(warning)
        self.assertEqual(items[0]["id"], "a" * 12)
        self.assertTrue(items[0]["stored_in_sql"])
        self.assertNotIn("content", items[0])

    @patch("app.mysql_store.save_clip", return_value="master-id")
    @patch("app.mysql_store.load_config", return_value={"database": "suyuan"})
    def test_extension_save_immediately_persists_to_sql(self, _config, save_clip):
        origin = "chrome-extension://" + "a" * 32
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(browser_imports, "DATA_DIR", Path(folder)), \
             patch.object(browser_imports, "FILE", Path(folder) / "imports.json"), \
             patch.object(browser_imports, "TOKEN_FILE", Path(folder) / "token.txt"):
            token = browser_imports.pairing_token()
            body = json.dumps({"url": "https://example.org/analysis", "title": "Long analysis",
                               "source": "Example", "published_at": "2026-11-07", "language": "en",
                               "content": "Independent analysis. " * 30}).encode("utf-8")
            connection = HTTPConnection("127.0.0.1", self.port, timeout=2)
            try:
                connection.request("POST", "/api/browser/import", body, {
                    "Content-Type": "application/json", "Origin": origin, "X-Suyuan-Import-Key": token})
            except PermissionError as error:
                self.skipTest(f"当前受限执行环境禁止本机回环请求：{error}")
            response = connection.getresponse()
            result = json.loads(response.read().decode("utf-8"))
            connection.close()
            self.assertEqual(response.status, 200)
            self.assertTrue(result["article"]["stored_in_sql"])
            self.assertEqual(save_clip.call_args.args[0]["content"], ("Independent analysis. " * 30).strip())

    def setUp(self):
        self.server = app.ThreadingHTTPServer(("127.0.0.1", 0), app.SuyuanHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_port

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def test_language_defaults_route_matches_search_policy(self):
        connection = HTTPConnection("127.0.0.1", self.port, timeout=2)
        try:
            try:
                connection.request("GET", "/api/search/languages")
            except PermissionError as error:
                self.skipTest(f"当前受限执行环境禁止本机回环请求：{error}")
            response = connection.getresponse()
            data = json.loads(response.read().decode("utf-8"))
            self.assertEqual(response.status, 200)
            self.assertEqual([item["code"] for item in data["default"]], list(app.DEFAULT_LANGUAGES))
            self.assertNotIn("vi", [item["code"] for item in data["supported"]])
            self.assertEqual(data["queries_per_language"], 2)
            self.assertEqual(response.getheader("Cache-Control"), "no-store")
        finally:
            connection.close()

    @patch("app.mysql_store.save_evidence_review")
    @patch("app.mysql_store.list_evidence_reviews", return_value=[])
    @patch("app.mysql_store.load_config", return_value={"database": "suyuan"})
    @patch("app.stored_run")
    def test_evidence_review_validates_passage_and_persists(self, stored_run, _config, _list, save):
        run_id = "a" * 12
        stored_run.return_value = {"id": run_id, "articles": [{"id": "A1", "passages": [{"id": "A1-P1", "text": "Verifiable source paragraph."}]}]}
        save.side_effect = lambda _config, _run, passage, digest, status, note: {
            "passage_id": passage, "excerpt_hash": digest, "status": status, "note": note}
        def request(path, payload):
            connection = HTTPConnection("127.0.0.1", self.port, timeout=2)
            try:
                connection.request("POST", path, json.dumps(payload).encode(), {"Content-Type": "application/json"})
                response = connection.getresponse()
                return response.status, json.loads(response.read().decode())
            finally:
                connection.close()
        try:
            status, _ = request(f"/api/evidence-reviews/{run_id}", {"passage_id": "A2-P1", "status": "verified"})
        except PermissionError as error:
            self.skipTest(f"当前受限执行环境禁止本机回环请求：{error}")
        self.assertEqual(status, 404)
        status, body = request(f"/api/evidence-reviews/{run_id}", {"passage_id": "A1-P1", "status": "verified", "note": "Checked"})
        self.assertEqual(status, 200)
        self.assertEqual(body["review"]["status"], "verified")
        self.assertEqual(save.call_args.args[4], "verified")
        status, body = request(f"/api/evidence-reviews/{run_id}", {"passage_id": "A1-P1", "status": "pending"})
        self.assertEqual(status, 200)
        self.assertEqual(body["review"]["status"], "pending")

    def test_paywall_markers_are_not_treated_as_public_text(self):
        self.assertTrue(app.is_restricted_content("Example", "Please subscribe to continue reading."))
        self.assertFalse(app.is_restricted_content("Example", "Public reporting is available."))

    def test_assessment_drops_unknown_evidence_ids(self):
        result = app.normalize_assessment(
            {
                "summary": "test",
                "risk_level": "高",
                "risk_reason": "test",
                "transmission_points": [{"label": "point", "why": "why", "evidence_ids": ["A1", "X9"]}],
                "narratives": [],
                "actions": ["review"],
                "limitations": [],
            },
            [{"id": "A1"}],
        )
        self.assertEqual(result["transmission_points"][0]["evidence_ids"], ["A1"])

    @patch("app.urlopen")
    def test_archive_returns_link_only(self, mocked_open):
        class FakeResponse:
            def read(self):
                return b'[["timestamp","original"],["20260101120000","https://example.com/a"]]'

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        mocked_open.return_value = FakeResponse()
        snapshot = app.archive_snapshot("https://example.com/a")
        self.assertEqual(snapshot["captured_at"], "20260101120000")
        self.assertIn("web.archive.org", snapshot["snapshot_url"])
        self.assertIn("filter=statuscode%3A200", mocked_open.call_args.args[0].full_url)
        self.assertIn("filter=mimetype%3Atext%2Fhtml", mocked_open.call_args.args[0].full_url)

    @patch("app.archive_snapshot", return_value={"captured_at": "20261015120000", "snapshot_url": "https://web.archive.org/web/20261015120000/https://example.com/a"})
    def test_fetch_timeout_keeps_public_snapshot_as_lead(self, looked_up):
        class FakeClient:
            def fetch(self, _url):
                raise TimeoutError("timed out")

        article = app.fetch_article(FakeClient(), {"url": "https://example.com/a", "title": "Article"}, 1)
        self.assertEqual(article["evidence_level"], "公开存档线索")
        self.assertIn("请求超时", article["access_note"])
        self.assertEqual(article["content"], "")
        looked_up.assert_called_once()

    @patch("app.archive_snapshot", return_value=None)
    def test_restricted_source_with_no_snapshot_is_not_full_text(self, looked_up):
        class FakeClient:
            def fetch(self, _url):
                raise app.ApiError(403, "Forbidden")

        article = app.fetch_article(FakeClient(), {"url": "https://example.com/a", "title": "Article"}, 1)
        self.assertEqual(article["evidence_level"], "访问受限")
        self.assertEqual(article["content"], "")
        looked_up.assert_called_once()

    @patch("app.archive_snapshot", return_value=None)
    def test_priority_original_can_use_attributed_public_version(self, _looked_up):
        class FakeClient:
            def fetch(self, url):
                if "reuters.com" in url:
                    raise app.ApiError(403, "Forbidden")
                return {"content": "Reuters original in-depth analysis. " * 130}

            def search(self, _query):
                return [{"url": "https://newspaper.example/analysis", "title": "An original policy analysis"}]

        article = app.fetch_article(
            FakeClient(), {"url": "https://www.reuters.com/world/analysis", "title": "An original policy analysis"}, 1)
        self.assertEqual(article["evidence_level"], "公开转载正文")
        self.assertEqual(article["content_url"], "https://newspaper.example/analysis")
        self.assertEqual(article["url"], "https://www.reuters.com/world/analysis")

    @patch("app.archive_snapshot", return_value={"captured_at": "20261015120000", "snapshot_url": "https://web.archive.org/web/20261015120000/https://www.reuters.com/world/analysis"})
    @patch("app.public_archive_text", return_value="Independent Reuters commentary. " * 140)
    def test_priority_original_can_use_public_archive_text(self, _archive_text, _looked_up):
        class FakeClient:
            def fetch(self, url):
                raise app.ApiError(403, "Forbidden")

        article = app.fetch_article(
            FakeClient(), {"url": "https://www.reuters.com/world/analysis", "title": "An original policy analysis"}, 1)
        self.assertEqual(article["evidence_level"], "公开存档正文")
        self.assertEqual(article["content_url"], article["archive"]["snapshot_url"])
        self.assertIn("公开历史快照", article["access_note"])

    def test_archive_html_parser_prefers_article_and_ignores_navigation(self):
        parser = app.ArticleHTMLParser()
        parser.feed("<body><nav>menu links</nav><main><article><h1>Analysis</h1><p>Independent argument.</p></article></main><footer>legal links</footer></body>")
        self.assertEqual(parser.text(), "Analysis Independent argument.")

    @patch("app.load_local_env", return_value={})
    @patch.dict("os.environ", {"DEEPSEEK_API_KEY": ""}, clear=False)
    def test_local_routes_serve_page_and_require_key_for_run(self, _mocked_config):
        connection = HTTPConnection("127.0.0.1", self.port, timeout=2)
        try:
            connection.request("GET", "/")
        except PermissionError as error:
            self.skipTest(f"当前受限执行环境禁止本机回环请求：{error}")
        response = connection.getresponse()
        page = response.read().decode("utf-8")
        self.assertEqual(response.status, 200)
        self.assertIn("涉华政经舆情风险研判", page)

        connection.request("GET", "/settings")
        response = connection.getresponse()
        settings_page = response.read().decode("utf-8")
        self.assertEqual(response.status, 200)
        self.assertIn('id="panel-deepseek"', settings_page)
        self.assertIn('id="panel-analysis"', settings_page)
        self.assertIn('id="panel-mysql"', settings_page)

        body = json.dumps({"task": "检索过去一个月人工智能监管政策的跨语种传播风险"}).encode("utf-8")
        connection.request("POST", "/api/run", body=body, headers={"Content-Type": "application/json"})
        response = connection.getresponse()
        self.assertEqual(response.status, 409)
        payload = json.loads(response.read().decode("utf-8"))
        self.assertIn("未配置 DeepSeek API Key", payload["error"])
        connection.close()

    @patch("app.load_local_env", side_effect=[{"DEEPSEEK_API_KEY": "first"}, {"DEEPSEEK_API_KEY": "second"}])
    def test_config_is_reloaded_for_each_client(self, _mocked_config):
        self.assertEqual(app.DeepSeekClient().api_key, "first")
        self.assertEqual(app.DeepSeekClient().api_key, "second")

    @patch("app.execute_research", return_value={"id": "test-run", "articles": [], "assessment": {}})
    @patch("app.DeepSeekClient")
    def test_background_run_endpoint_returns_completed_job(self, mocked_client, _mocked_research):
        mocked_client.return_value.configured = True
        connection = HTTPConnection("127.0.0.1", self.port, timeout=2)
        body = json.dumps({"task": "检索过去一个月人工智能监管政策的国际报道"}).encode("utf-8")
        try:
            connection.request("POST", "/api/run/start", body=body, headers={"Content-Type": "application/json"})
        except PermissionError as error:
            self.skipTest(f"当前受限执行环境禁止本机回环请求：{error}")
        response = connection.getresponse()
        self.assertEqual(response.status, 202)
        job_id = json.loads(response.read().decode("utf-8"))["job_id"]
        for _ in range(20):
            connection.request("GET", f"/api/jobs/{job_id}")
            result = json.loads(connection.getresponse().read().decode("utf-8"))
            if result["status"] == "done":
                break
            time.sleep(0.02)
        self.assertEqual(result["status"], "done")
        self.assertEqual(result["result"]["id"], "test-run")
        connection.close()

    def test_background_job_exposes_structured_progress(self):
        job_id = "a" * 12
        app.JOBS[job_id] = {"id": job_id, "status": "running", "progress": "等待开始"}

        def fake_research(_payload, progress):
            progress({"stage": "screen", "percent": 64, "message": "正在筛选", "qualified": 12})
            self.assertEqual(app.JOBS[job_id]["progress_detail"]["qualified"], 12)
            return {"id": "test-run", "articles": [], "assessment": {}}

        try:
            with patch.object(app, "execute_research", side_effect=fake_research):
                app.run_job(job_id, {})
            self.assertEqual(app.JOBS[job_id]["progress_detail"]["percent"], 100)
            self.assertEqual(app.JOBS[job_id]["status"], "done")
        finally:
            app.JOBS.pop(job_id, None)

    def test_analysis_config_routes_save_test_and_hide_key(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(analysis_store, "CONFIG_FILE", Path(directory) / "analysis-config.json"), \
             patch.object(app.AnalysisClient, "test_connection"):
            connection = HTTPConnection("127.0.0.1", self.port, timeout=2)
            payload = {"format": "anthropic", "base_url": "https://gateway.example/anthropic",
                       "api_key": "private-secret", "model": "custom-model"}
            try:
                connection.request("POST", "/api/analysis/test", body=json.dumps(payload),
                                   headers={"Content-Type": "application/json"})
            except PermissionError as error:
                self.skipTest(f"当前受限执行环境禁止本机回环请求：{error}")
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            response.read()
            connection.request("POST", "/api/analysis/config", body=json.dumps(payload),
                               headers={"Content-Type": "application/json"})
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            response.read()
            connection.request("GET", "/api/analysis/config")
            response = connection.getresponse()
            public = response.read().decode("utf-8")
            self.assertEqual(response.status, 200)
            self.assertIn("custom-model", public)
            self.assertNotIn("private-secret", public)
            connection.close()

    def test_watch_routes_save_toggle_and_start_incremental_job(self):
        run = {"id": "a" * 12, "task": "检索某议题的跨语种深度报道",
               "plan": {"topic": "某议题", "languages": ["en"],
                        "selection_policy": "representative_v2", "end_date": "2026-11-07"},
               "articles": [], "candidates": [{"url": "https://example.org/a"}]}
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(watch_store, "FILE", Path(directory) / "watches.json"), \
             patch.object(app, "stored_run", return_value=run), \
             patch.object(app, "DeepSeekClient") as crawler, \
             patch.object(app, "AnalysisClient") as analyst, \
             patch.object(app, "launch_job", return_value="b" * 12):
            crawler.return_value.configured = True
            analyst.return_value.configured = True
            connection = HTTPConnection("127.0.0.1", self.port, timeout=2)
            try:
                connection.request("POST", "/api/watches", body=json.dumps({"run_id": run["id"], "daily_time": "09:00"}),
                                   headers={"Content-Type": "application/json"})
            except PermissionError as error:
                self.skipTest(f"当前受限执行环境禁止本机回环请求：{error}")
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            watch = json.loads(response.read())["watch"]
            self.assertNotIn("seen_urls", watch)
            connection.request("GET", "/api/watches")
            self.assertEqual(len(json.loads(connection.getresponse().read())["watches"]), 1)
            connection.request("POST", f"/api/watches/{watch['id']}/run", body="{}",
                               headers={"Content-Type": "application/json"})
            response = connection.getresponse()
            self.assertEqual(response.status, 202)
            self.assertEqual(json.loads(response.read())["job_id"], "b" * 12)
            connection.request("POST", f"/api/watches/{watch['id']}/toggle", body='{"enabled":false}',
                               headers={"Content-Type": "application/json"})
            response = connection.getresponse()
            self.assertFalse(json.loads(response.read())["watch"]["enabled"])
            connection.close()


if __name__ == "__main__":
    unittest.main()
