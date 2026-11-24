import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import analysis_store
import deepseek_store
import app


class FakeResponse:
    def __init__(self, value):
        self.value = value

    def read(self, size=-1):
        return json.dumps(self.value).encode("utf-8")[:size] if size >= 0 else json.dumps(self.value).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class AnalysisProviderTests(unittest.TestCase):
    def test_deepseek_config_keeps_key_private_and_reuses_legacy_key(self):
        config = deepseek_store.validate_config({"api_key": "private-key", "model": "deepseek-flash",
                                                 "reasoning_effort": "max"})
        with tempfile.TemporaryDirectory() as directory, patch.object(deepseek_store, "CONFIG_FILE", Path(directory) / "deepseek-config.json"):
            deepseek_store.save_config(config)
            self.assertEqual(deepseek_store.load_config()["reasoning_effort"], "max")
            self.assertEqual(deepseek_store.validate_config({"api_key": "", "model": "deepseek-flash"}, config)["api_key"], "private-key")
        with self.assertRaises(ValueError):
            deepseek_store.validate_config({"api_key": "private-key", "model": "deepseek-flash", "reasoning_effort": "medium"})

    def test_model_urls_follow_configured_format(self):
        self.assertEqual(analysis_store.models_url("openai", "https://gateway.example/v1"), "https://gateway.example/v1/models")
        self.assertEqual(analysis_store.models_url("anthropic", "https://gateway.example/anthropic"), "https://gateway.example/anthropic/v1/models")

    @patch("app.urlopen", return_value=FakeResponse({"data": [{"id": "deepseek-flash", "name": "DeepSeek Flash",
                                                             "effort": {"supported_levels": ["low", "high", "max"], "default_level": "high"}}]}))
    def test_deepseek_model_catalogue_preserves_official_effort_levels(self, opened):
        models = app.list_provider_models("openai", "https://api.deepseek.com", "private-key")
        self.assertEqual(opened.call_args.args[0].full_url, "https://api.deepseek.com/models")
        self.assertEqual(models[0]["effort_levels"], ["low", "high", "max"])
        self.assertEqual(models[0]["default_effort"], "high")

    @patch("app.urlopen", return_value=FakeResponse({"data": [{"id": "claude-demo", "capabilities": {
        "effort": {"low": {"supported": True}, "xhigh": {"supported": False}, "max": {"supported": True}}}}]}))
    def test_anthropic_model_catalogue_uses_capabilities(self, _opened):
        models = app.list_provider_models("anthropic", "https://gateway.example", "private-key")
        self.assertEqual(models[0]["effort_levels"], ["low", "max"])

    def test_public_config_never_exposes_key_and_changed_host_needs_new_key(self):
        saved = analysis_store.validate_config({"format": "openai", "base_url": "https://gateway.example/v1",
                                                "model": "demo", "api_key": "secret-key"})
        with tempfile.TemporaryDirectory() as directory, patch.object(analysis_store, "CONFIG_FILE", Path(directory) / "analysis-config.json"):
            analysis_store.save_config(saved)
            public = analysis_store.public_config()
            self.assertNotIn("api_key", public)
            self.assertNotIn("secret-key", json.dumps(public))
            self.assertEqual(analysis_store.validate_config({**public, "api_key": ""}, saved)["api_key"], "secret-key")
            with self.assertRaises(ValueError):
                analysis_store.validate_config({**public, "base_url": "https://other.example/v1", "api_key": ""}, saved)

    @patch("app.urlopen", return_value=FakeResponse({"choices": [{"message": {"content": '{"ok":true}'}}]}))
    def test_openai_compatible_request_uses_chat_completions(self, opened):
        client = app.AnalysisClient({"format": "openai", "base_url": "https://gateway.example/v1",
                                     "api_key": "secret-key", "model": "demo-model"})
        result = client.post("/chat/completions", {"messages": [{"role": "user", "content": "hi"}],
                                                   "response_format": {"type": "json_object"}})
        request = opened.call_args.args[0]
        body = json.loads(request.data)
        self.assertEqual(request.full_url, "https://gateway.example/v1/chat/completions")
        self.assertEqual(body["model"], "demo-model")
        self.assertNotIn("response_format", body)
        self.assertEqual(result["choices"][0]["message"]["content"], '{"ok":true}')

    @patch("app.urlopen", return_value=FakeResponse({"content": [{"type": "text", "text": '{"ok":true}'}]}))
    def test_anthropic_compatible_request_moves_system_prompt(self, opened):
        client = app.AnalysisClient({"format": "anthropic", "base_url": "https://gateway.example/anthropic",
                                     "api_key": "secret-key", "model": "demo-model"})
        result = client.post("/chat/completions", {"messages": [
            {"role": "system", "content": "Return JSON"}, {"role": "user", "content": "hi"}]})
        request = opened.call_args.args[0]
        body = json.loads(request.data)
        headers = {key.lower(): value for key, value in request.header_items()}
        self.assertEqual(request.full_url, "https://gateway.example/anthropic/v1/messages")
        self.assertEqual(body["system"], "Return JSON")
        self.assertEqual(body["messages"], [{"role": "user", "content": "hi"}])
        self.assertEqual(headers["x-api-key"], "secret-key")
        self.assertEqual(result["choices"][0]["message"]["content"], '{"ok":true}')

    @patch("app.urlopen", return_value=FakeResponse({"choices": [{"message": {"content": "OK"}}]}))
    def test_openai_official_reasoning_effort_is_sent(self, opened):
        client = app.AnalysisClient({"format": "openai", "base_url": "https://gateway.example/v1",
                                     "api_key": "private-key", "model": "demo", "reasoning_effort": "xhigh"})
        client.test_connection()
        self.assertEqual(json.loads(opened.call_args.args[0].data)["reasoning_effort"], "xhigh")

    @patch("app.urlopen", return_value=FakeResponse({"content": [{"type": "text", "text": "OK"}]}))
    def test_anthropic_official_effort_is_sent(self, opened):
        client = app.AnalysisClient({"format": "anthropic", "base_url": "https://gateway.example",
                                     "api_key": "private-key", "model": "demo", "reasoning_effort": "max"})
        client.test_connection()
        body = json.loads(opened.call_args.args[0].data)
        self.assertEqual(body["thinking"], {"type": "adaptive"})
        self.assertEqual(body["output_config"], {"effort": "max"})

    @patch("app.urlopen", return_value=FakeResponse({"choices": [{"message": {"content": '{"ok":true}'}}]}))
    @patch("app.analysis_store.load_config", return_value=None)
    @patch("app.DeepSeekClient")
    def test_unconfigured_analysis_keeps_deepseek_default(self, deepseek_class, _load, opened):
        deepseek_class.return_value.api_key = "existing-deepseek-key"
        deepseek_class.return_value.model = "deepseek-flash"
        client = app.AnalysisClient()
        client.post("/chat/completions", {"messages": [{"role": "user", "content": "hi"}],
                                         "response_format": {"type": "json_object"}})
        request = opened.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.deepseek.com/chat/completions")
        self.assertIn("response_format", json.loads(request.data))


if __name__ == "__main__":
    unittest.main()
