import tempfile
import unittest
import io
from pathlib import Path
from unittest.mock import patch
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

import rag_store
import social_store
import paper_store


class RagStoreTests(unittest.TestCase):
    def test_index_search_update_and_grouping(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "index.sqlite3"
            docs = [
                {"kind": "news", "id": "a1", "title": "AI regulation", "content": "Policy risk and AI regulation in Europe. " * 15, "url": "https://example.org/a"},
                {"kind": "social", "id": "s1", "title": "", "content": "Community reaction to AI regulation and policy risk.", "url": "https://example.org/s"},
            ]
            self.assertEqual(rag_store.upsert_documents(docs, path)["indexed"], 2)
            self.assertEqual(rag_store.upsert_documents(docs, path)["unchanged"], 2)
            self.assertEqual(rag_store.stats(path)["social"], 1)
            self.assertEqual(rag_store.search("AI regulation", path=path, kinds=("social",))[0]["kind"], "social")
            docs[1]["content"] = "Updated public discussion about policy risk."
            self.assertEqual(rag_store.upsert_documents(docs, path)["indexed"], 1)
            self.assertEqual(rag_store.search("Community reaction", path=path, kinds=("social",)), [])
            self.assertEqual(rag_store.upsert_documents(docs[:1], path, prune=True)["removed"], 1)
            self.assertEqual(rag_store.stats(path)["social"], 0)

    def test_chinese_search(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "index.sqlite3"
            rag_store.upsert_documents([{"kind": "news", "id": "z1", "content": "跨语种风险叙事正在传播。", "url": "https://example.org/z"}], path)
            self.assertEqual(len(rag_store.search("风险叙事", path=path)), 1)

    def test_news_social_cross_language_vector_retrieval(self):
        import numpy as np
        class EmbeddingStub:
            def embed(self, texts, **_kwargs):
                return (np.array([1.0, 0.0], dtype=np.float32) if "tariff" in text.lower() or "关税" in text
                        else np.array([0.0, 1.0], dtype=np.float32) for text in texts)
        with tempfile.TemporaryDirectory() as directory, patch.object(rag_store, "_embedding_model", return_value=EmbeddingStub()):
            path = Path(directory) / "index.sqlite3"
            rag_store.upsert_documents([
                {"kind": "news", "id": "n1", "title": "Trade analysis", "content": "Tariff policy creates economic risk. " * 15},
                {"kind": "social", "id": "s1", "title": "Forum post", "content": "Tariff debate in a forum. " * 8},
            ], path)
            self.assertEqual(rag_store.search("关税", path=path, kinds=("news",)), [])
            status = rag_store.vectorize_news_social(path=path)
            self.assertTrue(status["complete"])
            self.assertEqual(rag_store.search_hybrid("关税", path=path, kinds=("news",))[0]["id"], "news:n1")
            self.assertEqual(rag_store.search_hybrid("关税", path=path, kinds=("social",))[0]["id"], "social:s1")
            self.assertEqual(rag_store.stats(path)["news_social_vectors"], status["total"])

    def test_paper_pages_and_vectors(self):
        import numpy as np
        class EmbeddingStub:
            def embed(self, texts, **_kwargs):
                return (np.array([1.0, 0.0], dtype=np.float32) if "trade" in text.lower()
                        else np.array([0.0, 1.0], dtype=np.float32) for text in texts)
        with tempfile.TemporaryDirectory() as directory, patch.object(rag_store, "_embedding_model", return_value=EmbeddingStub()):
            path = Path(directory) / "index.sqlite3"
            item = {"kind": "paper", "id": "p1", "title": "China trade research", "url": "/api/papers/p1/file",
                    "pages": ["Trade policy on China and tariffs. " * 20, "Investment and finance research. " * 20]}
            rag_store.upsert_documents([item], path)
            self.assertEqual(rag_store.stats(path)["paper"], 1)
            self.assertEqual(rag_store.search("tariffs", path=path, kinds=("paper",))[0]["page_no"], 1)
            self.assertTrue(rag_store.vectorize_paper("p1", path=path)["complete"])
            paper_hits = rag_store.search_papers("trade", path=path)
            self.assertEqual(paper_hits[0]["page_no"], 1)
            self.assertEqual(len({(hit["id"], hit["page_no"]) for hit in paper_hits}), len(paper_hits))
            self.assertEqual(rag_store.upsert_documents([], path, prune=True)["removed"], 1)
            self.assertEqual(rag_store.stats(path)["paper_vectors"], 0)

    def test_opposing_papers_remain_separate_in_retrieval(self):
        import numpy as np

        class EmbeddingStub:
            def embed(self, texts, **_kwargs):
                return (np.array([1.0, 0.0], dtype=np.float32) for _ in texts)

        with tempfile.TemporaryDirectory() as directory, patch.object(rag_store, "_embedding_model", return_value=EmbeddingStub()):
            path = Path(directory) / "index.sqlite3"
            docs = [
                {"kind": "paper", "id": "support", "title": "Tariff study A", "pages": ["Tariffs increase trade risk for China. " * 15]},
                {"kind": "paper", "id": "oppose", "title": "Tariff study B", "pages": ["Tariffs do not increase trade risk for China. " * 15]},
            ]
            rag_store.upsert_documents(docs, path)
            for paper in docs:
                rag_store.vectorize_paper(paper["id"], path=path)
            hits = rag_store.search_papers("tariff trade risk", limit=8, path=path)
            self.assertEqual({item["id"] for item in hits}, {"paper:support", "paper:oppose"})


class PaperStoreTests(unittest.TestCase):
    def test_scanned_pdf_uses_local_ocr_and_optional_vision_with_source_metadata(self):
        writer = PdfWriter()
        page = writer.add_blank_page(width=612, height=792)
        stream = DecodedStreamObject(); stream.set_data(b"q Q")
        page[NameObject("/Contents")] = writer._add_object(stream)
        output = io.BytesIO(); writer.write(output)
        with patch.object(paper_store, "_render_page", return_value=b"page-image"):
            paper = paper_store.extract_pdf(output.getvalue(), "scan.pdf",
                metadata_hint={"title": "Web title", "authors": "Alice; Bob", "year": "2024", "doi": "10.1000/xyz123"},
                doi_lookup=lambda doi: {"title": "Crossref title", "journal": "Policy Journal"},
                ocr_page=lambda image: ("low quality " * 8, 0.4),
                vision_page=lambda image, number: "Multimodal readable evidence. " * 12)
        self.assertEqual(paper["page_methods"], ["vision"])
        self.assertEqual(paper["title"], "Web title")
        self.assertEqual(paper["authors"], "Alice; Bob")
        self.assertEqual(paper["metadata"]["journal"], "Policy Journal")
        self.assertEqual(paper["metadata"]["sources"]["doi"], "web")

    def test_extract_text_pdf_and_reject_scanned_page(self):
        writer = PdfWriter()
        page = writer.add_blank_page(width=612, height=792)
        output = io.BytesIO()
        writer.write(output)
        with self.assertRaisesRegex(ValueError, "OCR"):
            paper_store.extract_pdf(output.getvalue(), "scan.pdf")
        font = writer._add_object(DictionaryObject({NameObject("/Type"): NameObject("/Font"),
                                                    NameObject("/Subtype"): NameObject("/Type1"),
                                                    NameObject("/BaseFont"): NameObject("/Helvetica")}))
        page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})})
        stream = DecodedStreamObject()
        stream.set_data(b"BT /F1 12 Tf 72 720 Td (China trade and policy research. ) Tj "
                        + b"(China trade and policy research. ) Tj " * 12 + b"ET")
        page[NameObject("/Contents")] = writer._add_object(stream)
        output = io.BytesIO(); writer.write(output)
        result = paper_store.extract_pdf(output.getvalue(), "trade.pdf", title="China Trade", year="2025")
        self.assertEqual(result["title"], "China Trade")
        self.assertEqual(result["year"], 2025)
        self.assertGreater(result["text_chars"], 200)


class SocialStoreTests(unittest.TestCase):
    def test_lemmy_public_rss_local_filter(self):
        from unittest.mock import MagicMock
        xml = b'''<rss><channel><item><title>China tariff policy debate</title>
          <link>https://lemmy.world/post/123</link>
          <description>submitted by alice to china&lt;br&gt;2 points | 0 comments&lt;br&gt;&lt;p&gt;A user discusses trade risks.&lt;/p&gt;</description>
          <pubDate>Fri, 20 Nov 2026 15:16:23 +0000</pubDate></item>
          <item><title>Unrelated game discussion</title><link>https://lemmy.world/post/124</link></item>
          </channel></rss>'''
        response = MagicMock()
        response.read.return_value = xml
        response.__enter__.return_value = response
        with patch.object(social_store, "urlopen", return_value=response):
            found = social_store.lemmy_rss("china", "tariff")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["id"], "lemmy:123")
        self.assertIn("2026-11-20", found[0]["created_at"])

    @patch.object(social_store, "_get_json")
    def test_hackernews_normalization(self, get_json):
        get_json.return_value = {"hits": [{"objectID": "123", "title": "AI &amp; policy", "story_text": "<p>Long discussion about public policy and the social impact of AI systems.</p>", "author": "alice", "points": 12, "num_comments": 4}]}
        post = social_store.hackernews("AI policy")[0]
        self.assertEqual(post["url"], "https://news.ycombinator.com/item?id=123")
        self.assertIn("AI & policy", post["content"])
        self.assertEqual(post["replies"], 4)

    @patch.object(social_store, "_get_json")
    def test_hackernews_comment_text(self, get_json):
        get_json.return_value = {"hits": [{"objectID": "456", "story_title": "AI policy", "comment_text": "<p>I think this rule will affect smaller teams because compliance costs are high.</p>", "author": "bob"}]}
        post = social_store.hackernews_comments("AI policy")[0]
        self.assertIn("compliance costs", post["content"])
        self.assertIn("Comment on", post["title"])

    @patch.object(social_store, "_get_json")
    def test_mastodon_normalization(self, get_json):
        get_json.return_value = [{"id": "22", "content": "<p>监管讨论</p>", "account": {"acct": "bob"}, "url": "https://mastodon.social/@bob/22", "favourites_count": 3}]
        post = social_store.mastodon_tag("AI")[0]
        self.assertEqual(post["content"], "监管讨论")
        self.assertEqual(post["likes"], 3)
        with self.assertRaises(ValueError):
            social_store.mastodon_tag("https://example.com/")


if __name__ == "__main__":
    unittest.main()
