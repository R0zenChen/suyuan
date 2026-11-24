import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import meltwater_source as source


class MeltwaterSourceTests(unittest.TestCase):
    def test_validate_search_keeps_full_mode_and_rejects_bad_range(self):
        result = source.validate_search({"query": '"China tariffs"', "from": "2026-01-01",
                                         "to": "2026-01-02", "kind": "news", "account_key": "default"})
        self.assertEqual(result["mode"], "full")
        self.assertEqual(result["accountKey"], "default")
        with self.assertRaisesRegex(ValueError, "370"):
            source.validate_search({"query": "China", "from": "2025-01-01", "to": "2026-11-24"})

    def test_restricted_and_non_news_types_keep_their_evidence_level(self):
        item = {"id": "doc-1", "title": source.REDACTED_TITLE, "url": "https://example.com",
                "type": "news", "excerpt": "", "source": "Publisher"}
        self.assertEqual(source.normalize_mention(item, "query", "a" * 12)["restricted"], 1)
        item.update(title="Post", type="tender", excerpt="A public procurement notice")
        self.assertEqual(source.normalize_mention(item, "query", "a" * 12)["kind"], "tender")

    def test_export_checks_platform_count_and_unique_document_ids(self):
        lines = [json.dumps({"id": value, "title": value, "date": "2026-01-01"}).encode() + b"\n"
                 for value in ("a", "b")]
        with tempfile.TemporaryDirectory() as directory, patch.object(source, "DOWNLOAD_DIR", Path(directory)):
            with patch.object(source, "urlopen", return_value=io.BytesIO(b"".join(lines))):
                file, rows = source.download_export("a" * 12, 2)
            self.assertEqual(rows, 2)
            self.assertEqual(file.read_bytes(), b"".join(lines))
            with patch.object(source, "urlopen", return_value=io.BytesIO(b"".join(lines))):
                with self.assertRaisesRegex(source.StudioError, "平台报告"):
                    source.download_export("b" * 12, 3)
            self.assertFalse((Path(directory) / ("b" * 12 + ".jsonl")).exists())
            with patch.object(source, "urlopen", return_value=io.BytesIO(lines[0] * 2)):
                with self.assertRaisesRegex(source.StudioError, "重复文档"):
                    source.download_export("c" * 12, 2)


if __name__ == "__main__":
    unittest.main()
