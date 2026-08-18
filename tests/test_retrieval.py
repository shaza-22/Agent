"""Phase 2 retrieval tests. Offline: uses the hashing embedder, no downloads.

Run: python -m unittest discover -s tests -v
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from retrieval.bm25 import BM25, normalise_arabic, tokenize      # noqa: E402
from retrieval.cache import EmbeddingCache                        # noqa: E402
from retrieval.embedders import build_embedder                    # noqa: E402
from retrieval.index import VectorIndex                           # noqa: E402
from retrieval.records import (chunk_pdf_text, load_all,          # noqa: E402
                               load_html_records, load_pdf_records)
from retrieval.retriever import Retriever                         # noqa: E402

BASE = "https://fixture.example/"


def _doc(url, title, lang, sections, tables=None):
    return {"url": url, "source_url": url + "?csrt=1", "final_url": url,
            "title": title, "language": lang, "fetched_at": "2026-08-18T00:00:00Z",
            "content_hash": "h", "breadcrumbs": [], "section_path": [],
            "text": " ".join(s["text"] for s in sections), "sections": sections,
            "tables": tables or [], "links": [], "pdf_links": [], "doc_type": "page",
            "word_count": 50}


def _sec(heading, text, anchor=None):
    return {"heading": heading, "level": 2, "text": text, "anchor": anchor}


def write_corpus(dirpath: Path) -> None:
    dirpath.mkdir(parents=True, exist_ok=True)
    docs = [
        _doc(BASE + "cards/gold", "Gold Credit Card", "en", [
            _sec("Eligibility", "Applicants need a minimum monthly income of "
                                "EGP 8000 and a valid national identity card.",
                 "eligibility"),
            _sec("Fees", "The annual fee for the Gold card is EGP 350.", "fees")],
             [{"caption": None, "headers": ["Item", "Amount"],
               "rows": [["Annual fee", "EGP 350"]],
               "markdown": "| Item | Amount |\n| --- | --- |\n| Annual fee | EGP 350 |"}]),
        _doc(BASE + "accounts/current", "Current Account", "en", [
            _sec("Required Documents", "To open an account provide a national "
                                       "identity card and proof of address.",
                 "documents")]),
        _doc(BASE + "ar/cards/gold", "بطاقة الائتمان الذهبية", "ar", [
            _sec("الرسوم", "الرسوم السنوية لبطاقة الائتمان الذهبية 350 جنيه مصري",
                 "fees")]),
    ]
    with (dirpath / "documents.jsonl").open("w", encoding="utf-8") as fh:
        for d in docs:
            fh.write(json.dumps(d, ensure_ascii=False) + "\n")

    pdf_text = ("Tariff of fees for payment cards.\n\n"
                "Credit card annual fee Classic EGP 150 Gold EGP 350.\n\n"
                "Cash withdrawal commission is two percent of the amount drawn.")
    pdfs = [
        {"url": BASE + "docs/tariff.pdf", "source_url": BASE + "docs/tariff.pdf",
         "title": "tariff.pdf", "language": "en", "text": pdf_text,
         "fetched_at": "2026-08-18T00:00:00Z", "pdf_pages": 2, "doc_type": "pdf",
         "needs_ocr": False, "attachment_kind": "pdf"},
        # Scanned attachment: exists, but has no extractable text.
        {"url": BASE + "docs/scanned.ashx", "source_url": BASE + "docs/scanned.ashx",
         "title": "scanned.ashx", "language": "unknown", "text": "",
         "fetched_at": "2026-08-18T00:00:00Z", "pdf_pages": 1, "doc_type": "pdf",
         "needs_ocr": True, "attachment_kind": "image"},
    ]
    with (dirpath / "pdf_documents.jsonl").open("w", encoding="utf-8") as fh:
        for d in pdfs:
            fh.write(json.dumps(d, ensure_ascii=False) + "\n")


class RetrievalFixture(unittest.TestCase):
    """Builds a real FAISS index over a fixture corpus, once per class."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        cls.corpus = root / "corpus"
        cls.index_dir = root / "retrieval"
        cls.cache_dir = cls.index_dir / "cache"
        write_corpus(cls.corpus)

        cls.records = load_all(cls.corpus)
        embedder = build_embedder("hashing")
        idx = VectorIndex(cls.index_dir)
        cls.stats = idx.build(cls.records, embedder, cache_dir=cls.cache_dir)
        idx.save()
        cls.retriever = Retriever(cls.index_dir)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()


class TestRecordLoading(RetrievalFixture):
    def test_html_sections_become_records(self):
        html = [r for r in self.records if r.document_type == "html"]
        self.assertEqual(len(html), 4)   # 2 gold + 1 current + 1 arabic

    def test_table_markdown_is_indexed_with_the_section(self):
        # Fees live in tables; if the markdown is dropped they are unretrievable.
        gold = [r for r in self.records if "gold" in r.source_url and r.language == "en"]
        self.assertTrue(any("| Annual fee | EGP 350 |" in r.text for r in gold))

    def test_scanned_pdf_is_not_indexed_as_content(self):
        self.assertFalse(any("scanned" in r.source_url for r in self.records))

    def test_pdf_chunks_exist_and_carry_page_numbers(self):
        pdfs = [r for r in self.records if r.document_type == "pdf"]
        self.assertTrue(pdfs)
        self.assertTrue(all(r.pdf_page is not None for r in pdfs))
        self.assertTrue(all(r.extra.get("page_is_estimated") for r in pdfs))

    def test_chunk_ids_are_stable_across_reloads(self):
        again = load_all(self.corpus)
        self.assertEqual([r.chunk_id for r in self.records],
                         [r.chunk_id for r in again])

    def test_pdf_chunker_overlaps_and_respects_size(self):
        text = "\n\n".join(f"Paragraph {i} " + "word " * 60 for i in range(8))
        chunks = chunk_pdf_text(text, target_words=100, overlap_words=20)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(c.split()) <= 260 for c in chunks))

    def test_empty_corpus_directory_yields_no_records(self):
        with tempfile.TemporaryDirectory() as empty:
            self.assertEqual(load_all(Path(empty)), [])


class TestIndexAndCache(RetrievalFixture):
    def test_index_files_persist(self):
        for name in (VectorIndex.INDEX_FILE, VectorIndex.RECORDS_FILE,
                     VectorIndex.MANIFEST_FILE):
            self.assertTrue((self.index_dir / name).exists(), name)

    def test_manifest_records_the_model_for_reproducibility(self):
        m = json.loads((self.index_dir / VectorIndex.MANIFEST_FILE)
                       .read_text(encoding="utf-8"))
        self.assertIn("embedder", m)
        self.assertEqual(m["embedder"]["dimension"], 384)
        self.assertIn("faiss", m["index_type"].lower())

    def test_first_build_embeds_everything(self):
        self.assertEqual(self.stats.embedded_now, len(self.records))
        self.assertEqual(self.stats.reused_from_cache, 0)

    def test_rebuild_reuses_cached_embeddings(self):
        embedder = build_embedder("hashing")
        idx = VectorIndex(self.index_dir)
        stats = idx.build(self.records, embedder, cache_dir=self.cache_dir)
        self.assertEqual(stats.embedded_now, 0)
        self.assertEqual(stats.reused_from_cache, len(self.records))

    def test_cache_survives_a_corrupt_file_without_crashing(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "deadbeef1234.npz"
            bad.write_text("not an npz file", encoding="utf-8")
            cache = EmbeddingCache(Path(tmp), "deadbeef1234", 384)
            _, missing = cache.get_many(["a", "b"])
            self.assertEqual(missing, [0, 1])

    def test_loading_a_missing_index_is_a_clear_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(FileNotFoundError):
                VectorIndex.load(Path(tmp))


class TestRetrievalApi(RetrievalFixture):
    def test_returns_results_with_full_provenance(self):
        hits = self.retriever.retrieve("annual fee gold card", top_k=3)
        self.assertTrue(hits)
        required = ("source_url", "title", "section_heading", "text", "language",
                    "score", "document_type", "chunk_id", "citation", "rank")
        for field in required:
            self.assertIn(field, hits[0], field)

    def test_citation_includes_the_section_anchor(self):
        hits = self.retriever.retrieve("annual fee gold card", top_k=5)
        anchored = [h for h in hits if "#" in h["citation"]]
        self.assertTrue(anchored, "no result carried a deep-link anchor")
        self.assertTrue(anchored[0]["citation"].startswith(anchored[0]["source_url"]))

    def test_top_k_is_respected(self):
        self.assertLessEqual(len(self.retriever.retrieve("card", top_k=2)), 2)

    def test_language_filter_returns_only_that_language(self):
        for lang in ("en", "ar"):
            hits = self.retriever.retrieve("رسوم بطاقة fee card", top_k=10,
                                           language=lang)
            self.assertTrue(hits, f"no {lang} results")
            self.assertTrue(all(h["language"] == lang for h in hits))

    def test_document_type_filter(self):
        hits = self.retriever.retrieve("annual fee", top_k=10, document_type="pdf")
        self.assertTrue(hits)
        self.assertTrue(all(h["document_type"] == "pdf" for h in hits))
        self.assertTrue(all(h["pdf_page"] for h in hits))

    def test_empty_query_returns_nothing(self):
        self.assertEqual(self.retriever.retrieve("", top_k=5), [])
        self.assertEqual(self.retriever.retrieve("   ", top_k=5), [])

    def test_nonsense_query_does_not_crash(self):
        # May return low-scoring results; must never raise.
        self.assertIsInstance(self.retriever.retrieve("zzzz qqqq", top_k=3), list)

    def test_language_filter_with_no_matches_returns_empty(self):
        hits = self.retriever.retrieve("annual fee", top_k=5, language="ar",
                                       document_type="pdf")
        self.assertEqual(hits, [])

    def test_max_per_document_diversifies_broad_queries(self):
        hits = self.retriever.retrieve("credit cards", top_k=6, max_per_document=1)
        urls = [h["source_url"] for h in hits]
        self.assertEqual(len(urls), len(set(urls)))

    def test_bm25_only_mode_needs_no_embedding_model(self):
        hits = self.retriever.retrieve("annual fee", top_k=3, mode="bm25")
        self.assertTrue(hits)
        self.assertTrue(all(h["bm25_score"] is not None for h in hits))

    def test_ranks_are_sequential(self):
        hits = self.retriever.retrieve("card fee", top_k=4)
        self.assertEqual([h["rank"] for h in hits], list(range(1, len(hits) + 1)))


class TestArabicLexicalMatching(unittest.TestCase):
    def test_normalisation_unifies_alef_and_ta_marbuta(self):
        self.assertEqual(normalise_arabic("البطاقة الأساسية"),
                         normalise_arabic("البطاقه الاساسيه"))

    def test_arabic_punctuation_is_not_glued_to_tokens(self):
        self.assertIn("مصر", tokenize("ما هي رسوم بطاقات بنك مصر؟"))

    def test_arabic_indic_digits_normalise_to_ascii(self):
        self.assertIn("350", tokenize("رسوم ٣٥٠ جنيه"))

    def test_bm25_matches_across_spelling_variants(self):
        docs = [tokenize("الرسوم السنوية للبطاقة الذهبية"),
                tokenize("فروع البنك وماكينات الصراف الآلي")]
        hits = BM25(docs).search("الرسوم السنوية للبطاقه")
        self.assertTrue(hits)
        self.assertEqual(hits[0][0], 0)
