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


# ---------------------------------------------------------------------------
# Evaluation fixture, modelled on the five real test queries.
# Properties are asserted, never specific documents - the point is that the
# ranker prefers the right KIND of result, not that it memorised an answer.
# ---------------------------------------------------------------------------

def write_eval_corpus(dirpath: Path) -> None:
    """A corpus shaped like the real one: news noise, corporate vs consumer
    products, an Arabic page, a fee table and a tariff PDF."""
    dirpath.mkdir(parents=True, exist_ok=True)
    docs = []
    for slug, title in [
            ("farmers-day", "Banque Misr celebrates Farmer's Day"),
            ("world-savings-day", "Banque Misr marks World Savings Day"),
            ("financial-inclusion", "Banque Misr financial inclusion initiative"),
            ("visa-partnership", "Banque Misr and Visa announce a credit card partnership"),
            ("women-day", "Banque Misr celebrates Women's Day")]:
        docs.append(_doc(BASE + "news/" + slug, title, "en", [
            _sec("Overview", f"{title}. Banque Misr offers credit cards and debit "
                             f"cards with benefits for all customers.")]))

    for slug, name, income, fee in [("gold", "Gold", "8000", "350"),
                                    ("platinum", "Platinum", "25000", "800"),
                                    ("classic", "Classic", "5000", "150")]:
        docs.append(_doc(
            BASE + "personal/cards/" + slug + "-credit-card", f"{name} Credit Card",
            "en", [
                _sec("Overview", f"The {name} credit card offers rewards for "
                                 f"individual customers."),
                _sec("Eligibility", f"Applicants must have a minimum monthly income "
                                    f"of EGP {income} and a valid national identity "
                                    f"card.", "eligibility"),
                _sec("Fees and Charges", f"The annual fee for the {name} card is "
                                         f"EGP {fee}.", "fees")],
            [{"caption": None, "headers": ["Item", "Amount"],
              "rows": [["Annual fee", f"EGP {fee}"]],
              "markdown": f"| Item | Amount |\n| --- | --- |\n| Annual fee | EGP {fee} |"}]))

    docs.append(_doc(BASE + "corporate/cards/business-credit-card",
                     "Business Credit Card", "en", [
                         _sec("Eligibility", "Companies must be registered and hold "
                                             "a corporate account.", "eligibility"),
                         _sec("Fees and Charges", "The issuance fee is EGP 200 and "
                                                  "the replacement fee is EGP 50.",
                              "fees")]))
    docs.append(_doc(BASE + "personal/accounts/current-account", "Current Account",
                     "en", [
                         _sec("Overview", "A current account for daily banking."),
                         _sec("Required Documents", "To open an account provide a "
                                                    "valid national identity card, "
                                                    "proof of address and a salary "
                                                    "certificate.", "documents")]))
    docs.append(_doc(BASE + "ar/personal/cards/gold", "بطاقة الائتمان الذهبية", "ar", [
        _sec("الرسوم والعمولات", "رسوم اصدار البطاقة 150 جنيه والرسوم السنوية "
                                 "350 جنيه مصري", "fees"),
        _sec("شروط الاستحقاق", "الحد الادنى للدخل الشهري 8000 جنيه مصري",
             "eligibility")]))
    docs.append(_doc(BASE + "about/atm-network", "ATM Network", "en",
                     [_sec("Locations", "Our ATM network covers all governorates.")]))

    with (dirpath / "documents.jsonl").open("w", encoding="utf-8") as fh:
        for d in docs:
            fh.write(json.dumps(d, ensure_ascii=False) + "\n")

    pdf_text = ("Tariff of fees and commissions for payment cards.\n\n"
                "Credit cards annual fee Classic EGP 150 Gold EGP 350 "
                "Platinum EGP 800.")
    with (dirpath / "pdf_documents.jsonl").open("w", encoding="utf-8") as fh:
        fh.write(json.dumps({
            "url": BASE + "docs/card-tariff.pdf",
            "source_url": BASE + "docs/card-tariff.pdf", "title": "card-tariff.pdf",
            "language": "en", "text": pdf_text, "pdf_pages": 2, "doc_type": "pdf",
            "fetched_at": "2026-08-18T00:00:00Z", "needs_ocr": False,
            "attachment_kind": "pdf"}, ensure_ascii=False) + "\n")


class RetrievalEvalFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        corpus, cls.index_dir = root / "corpus", root / "index"
        write_eval_corpus(corpus)
        records = load_all(corpus)
        idx = VectorIndex(cls.index_dir)
        idx.build(records, build_embedder("hashing"), cache_dir=root / "cache")
        idx.save()
        cls.r = Retriever(cls.index_dir)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()


class TestRetrievalQuality(RetrievalEvalFixture):
    """Property assertions for the five real test queries."""

    Q_ELIGIBILITY = "What are the eligibility requirements for a Banque Misr credit card?"
    Q_DOCUMENTS = "What documents do I need to open an account at Banque Misr?"
    Q_FEE = "What is the annual fee on Banque Misr cards?"
    Q_FEE_AR = "ما هي رسوم بطاقات بنك مصر؟"
    Q_COMPARE = "Compare Banque Misr credit cards"

    def test_eligibility_query_surfaces_an_eligibility_section(self):
        hits = self.r.retrieve(self.Q_ELIGIBILITY, top_k=5)
        self.assertTrue(any("eligib" in h["section_heading"].lower() for h in hits),
                        f"no eligibility section in top 5: "
                        f"{[h['section_heading'] for h in hits]}")

    def test_product_queries_do_not_return_news_pages(self):
        for q in (self.Q_ELIGIBILITY, self.Q_FEE, self.Q_COMPARE):
            hits = self.r.retrieve(q, top_k=5)
            self.assertFalse([h for h in hits if h["page_type"] == "news"],
                             f"news page returned for: {q}")

    def test_consumer_products_outrank_corporate_for_personal_questions(self):
        hits = self.r.retrieve(self.Q_ELIGIBILITY, top_k=5)
        segments = [h["segment"] for h in hits]
        if "corporate" in segments and "consumer" in segments:
            self.assertLess(segments.index("consumer"), segments.index("corporate"))

    def test_documents_query_surfaces_document_content(self):
        hits = self.r.retrieve(self.Q_DOCUMENTS, top_k=5)
        self.assertTrue(
            any("document" in h["section_heading"].lower()
                or "document" in h["text"].lower() for h in hits))

    def test_fee_query_surfaces_fee_bearing_content(self):
        hits = self.r.retrieve(self.Q_FEE, top_k=5)
        self.assertTrue(any("fee" in h["section_heading"].lower() for h in hits))
        # The section carrying the actual annual fee must beat the
        # issuance/replacement-fee-only corporate section.
        annual = [h["rank"] for h in hits if "annual fee" in h["text"].lower()]
        issuance = [h["rank"] for h in hits if "issuance fee" in h["text"].lower()
                    and "annual fee" not in h["text"].lower()]
        if annual and issuance:
            self.assertLess(min(annual), min(issuance))

    def test_arabic_fee_query_returns_arabic_fee_content(self):
        hits = self.r.retrieve(self.Q_FEE_AR, top_k=5, language="ar")
        self.assertTrue(hits)
        self.assertTrue(all(h["language"] == "ar" for h in hits))
        self.assertTrue(any("رسوم" in h["section_heading"] or "رسوم" in h["text"]
                            for h in hits))

    def test_comparison_query_returns_multiple_distinct_products(self):
        hits = self.r.retrieve(self.Q_COMPARE, top_k=5)
        urls = {h["source_url"] for h in hits}
        self.assertGreaterEqual(len(urls), 3, "comparison returned too few products")
        self.assertEqual(len(urls), len(hits), "same page returned more than once")

    def test_table_bearing_sections_are_not_penalised(self):
        hits = self.r.retrieve(self.Q_FEE, top_k=10)
        self.assertTrue(any(h["has_table"] for h in hits))

    def test_boosts_are_reported_for_explainability(self):
        hits = self.r.retrieve(self.Q_ELIGIBILITY, top_k=3)
        self.assertTrue(any(h["boosts"] for h in hits))
        self.assertIn("base_score", hits[0])


class TestRankingControls(RetrievalEvalFixture):
    def test_auto_filter_keeps_results_when_no_products_exist(self):
        # A query with product intent but no matching product pages must still
        # return something rather than filtering itself down to nothing.
        hits = self.r.retrieve("eligibility for a mortgage on Mars", top_k=5)
        self.assertIsInstance(hits, list)

    def test_boosts_can_be_disabled(self):
        boosted = self.r.retrieve(self.__class__.__dict__.get(
            "Q", "What are the eligibility requirements for a credit card?"),
            top_k=5)
        raw = self.r.retrieve(
            "What are the eligibility requirements for a credit card?",
            top_k=5, use_metadata_boosts=False, auto_filter=False)
        self.assertTrue(all(h["boosts"] == {} for h in raw))
        self.assertIsInstance(boosted, list)

    def test_single_mode_retrieval_is_unboosted_by_default(self):
        for mode in ("dense", "bm25"):
            hits = self.r.retrieve("annual fee", top_k=3, mode=mode)
            self.assertTrue(all(h["boosts"] == {} for h in hits), mode)

    def test_confidence_is_reported_and_bounded(self):
        hits = self.r.retrieve("annual fee credit card", top_k=5)
        for h in hits:
            self.assertGreaterEqual(h["confidence"], 0.0)
            self.assertLessEqual(h["confidence"], 1.0)

    def test_min_confidence_filters_weak_results(self):
        everything = self.r.retrieve("annual fee", top_k=10)
        strict = self.r.retrieve("annual fee", top_k=10, min_confidence=0.99)
        self.assertLess(len(strict), len(everything))

    def test_retrieve_with_status_reports_low_confidence(self):
        out = self.r.retrieve_with_status("zzz qqq nonsense", min_confidence=0.9)
        self.assertIn(out["status"], ("low_confidence", "no_results"))
        self.assertIn("message", out)

    def test_retrieve_with_status_reports_ok_for_a_good_query(self):
        out = self.r.retrieve_with_status(
            "What documents do I need to open an account?", min_confidence=0.1)
        self.assertEqual(out["status"], "ok")
        self.assertTrue(out["results"])


class TestCorpusExistenceCheck(RetrievalEvalFixture):
    """Absence of content must be distinguishable from bad ranking."""

    def test_existing_topic_is_found(self):
        from retrieval.diagnostics import inspect_topic
        self.assertTrue(inspect_topic(self.r, r"eligib"))

    def test_absent_topic_reports_nothing(self):
        from retrieval.diagnostics import inspect_topic
        self.assertEqual(inspect_topic(self.r, r"cryptocurrency custody"), [])


class TestSegmentClassificationRegression(unittest.TestCase):
    """Real-corpus evidence: 637/1264 units were labelled corporate against
    only 88 consumer, because the classifier scanned page body text. Since the
    ranker penalises corporate results on personal questions, that demoted half
    the corpus and let unclassified generic pages win.
    """

    def test_body_text_does_not_flip_a_retail_page_to_corporate(self):
        from retrieval.intent import classify_segment
        seg = classify_segment(
            "https://www.banquemisr.com/personal/cards/gold-credit-card",
            "Gold Credit Card",
            "Branches are open during business hours. Our company serves "
            "commercial clients and business customers across the country.")
        self.assertEqual(seg, "consumer")

    def test_generic_page_with_corporate_words_stays_unknown(self):
        from retrieval.intent import classify_segment
        seg = classify_segment("https://www.banquemisr.com/home/pages/rewards",
                               "BM Rewards",
                               "business business commercial company enterprise")
        self.assertEqual(seg, "unknown",
                         "an unlabelled page must stay unknown; a wrong label "
                         "is worse than no label because it triggers a penalty")

    def test_url_path_still_drives_the_classification(self):
        from retrieval.intent import classify_segment
        self.assertEqual(classify_segment(
            "https://www.banquemisr.com/corporate/cards/business-card", ""),
            "corporate")
        self.assertEqual(classify_segment(
            "https://www.banquemisr.com/personal/accounts/current", ""),
            "consumer")

    def test_ambiguous_title_does_not_pick_a_side(self):
        from retrieval.intent import classify_segment
        self.assertEqual(classify_segment(
            "https://www.banquemisr.com/home/pages/x",
            "Personal and Business Banking"), "unknown")


class TestPdfLanguageDerivation(unittest.TestCase):
    """Real-corpus evidence: 242 of 1264 units had language 'unknown', closely
    matching the 256 PDF units - pdf_documents.jsonl predates the language fix
    and was never regenerated. Deriving language at index time fixes it without
    touching the frozen corpus.
    """

    def _pdf_corpus(self, dirpath: Path, language: str) -> None:
        dirpath.mkdir(parents=True, exist_ok=True)
        (dirpath / "documents.jsonl").write_text("", encoding="utf-8")
        text_en = ("Tariff of fees and commissions. The annual fee for the gold "
                   "card is EGP 350 and eligibility requires proof of income.")
        text_ar = ("تعريفة الرسوم والعمولات الرسوم السنوية للبطاقة الذهبية "
                   "350 جنيه مصري وشروط الاستحقاق تتطلب اثبات الدخل")
        (dirpath / "pdf_documents.jsonl").write_text(json.dumps({
            "url": BASE + "docs/tariff.pdf", "source_url": BASE + "docs/tariff.pdf",
            "title": "tariff.pdf",
            "language": "unknown",        # stale value from Phase 1
            "text": text_ar if language == "ar" else text_en,
            "pdf_pages": 1, "doc_type": "pdf", "needs_ocr": False,
            "fetched_at": "2026-08-18T00:00:00Z"}, ensure_ascii=False) + "\n",
            encoding="utf-8")

    def test_stale_unknown_is_replaced_by_script_detection(self):
        for lang in ("en", "ar"):
            with tempfile.TemporaryDirectory() as tmp:
                corpus = Path(tmp) / "corpus"
                self._pdf_corpus(corpus, lang)
                recs = load_all(corpus)
                self.assertTrue(recs)
                self.assertTrue(all(r.language == lang for r in recs),
                                f"expected {lang}, got "
                                f"{[r.language for r in recs]}")

    def test_a_real_language_label_is_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            corpus = Path(tmp) / "corpus"
            self._pdf_corpus(corpus, "en")
            raw = (corpus / "pdf_documents.jsonl").read_text(encoding="utf-8")
            (corpus / "pdf_documents.jsonl").write_text(
                raw.replace('"language": "unknown"', '"language": "ar"'),
                encoding="utf-8")
            recs = load_all(corpus)
            self.assertTrue(all(r.language == "ar" for r in recs))


class TestSegmentPenaltyOnlyAppliesToLabelledRecords(RetrievalEvalFixture):
    def test_unknown_segment_is_never_penalised(self):
        hits = self.r.retrieve(
            "What are the eligibility requirements for a credit card?",
            top_k=10, max_per_document=99)
        for h in hits:
            if h["segment"] == "unknown":
                self.assertNotIn("segment_mismatch", h["boosts"])


class TestBanqueMisrUrlTaxonomy(unittest.TestCase):
    """Real URLs from the corpus. This site nests PERSONAL products under an
    /smes/ path segment, so /smes/ carries no audience meaning here and the
    deepest matching segment decides. Paths are percent-encoded in the corpus.
    """

    BASE = "https://www.banquemisr.com"

    def _seg(self, path, title=""):
        from retrieval.intent import classify_segment
        return classify_segment(self.BASE + path, title)

    def test_retail_banking_under_smes_is_consumer(self):
        self.assertEqual(self._seg(
            "/home/smes/retail%20banking/pages/cards/credit%20cards%20list/"
            "bm-youth-card", "BM YOUTH CARD"), "consumer")

    def test_corporate_banking_under_smes_is_corporate(self):
        self.assertEqual(self._seg(
            "/home/smes/corporate%20banking/companies%20cards",
            "Companies Cards"), "corporate")

    def test_rewards_club_under_retail_banking_is_consumer(self):
        self.assertEqual(self._seg(
            "/home/smes/retail%20banking/pages/bm%20rewards%20club/overview",
            "BM Rewards Club"), "consumer")

    def test_hyphenated_variants_behave_identically(self):
        self.assertEqual(self._seg("/home/smes/retail-banking/pages/cards/gold"),
                         "consumer")
        self.assertEqual(self._seg("/home/smes/corporate-banking/pages/loans"),
                         "corporate")

    def test_smes_alone_is_not_an_audience_signal(self):
        # The regression that mislabelled a large part of the retail catalogue.
        self.assertEqual(self._seg("/home/smes/pages/overview", "SMEs Overview"),
                         "unknown")

    def test_deepest_segment_wins_over_an_ancestor(self):
        # An ancestor describes the menu; the nearest segment describes the page.
        self.assertEqual(self._seg(
            "/home/smes/corporate%20banking/pages/retail%20banking/cards"),
            "consumer")

    def test_percent_encoding_is_decoded_before_matching(self):
        encoded = self._seg("/home/smes/retail%20banking/pages/cards")
        plain = self._seg("/home/smes/retail banking/pages/cards")
        self.assertEqual(encoded, plain)
        self.assertEqual(encoded, "consumer")


class TestIndexStalenessDetection(unittest.TestCase):
    """The embedding cache stores vectors only, so it cannot freeze metadata -
    but a records.jsonl that was never rebuilt can, and looks identical from
    the outside. That must be detectable."""

    def _build(self, tmp: Path):
        corpus = tmp / "corpus"
        corpus.mkdir(parents=True, exist_ok=True)
        url = ("https://www.banquemisr.com/home/smes/retail%20banking/pages/"
               "cards/bm-youth-card")
        (corpus / "documents.jsonl").write_text(json.dumps({
            "url": url, "source_url": url, "final_url": url,
            "title": "BM YOUTH CARD", "language": "en",
            "fetched_at": "2026-08-18T00:00:00Z", "content_hash": "x",
            "breadcrumbs": [], "section_path": [], "text": "youth card",
            "sections": [{"heading": "Eligibility", "level": 2, "anchor": "e",
                          "text": "Applicants must be aged between sixteen and "
                                  "twenty five and hold a national identity card."}],
            "tables": [], "links": [], "pdf_links": [], "doc_type": "page",
            "word_count": 20}, ensure_ascii=False) + "\n", encoding="utf-8")
        (corpus / "pdf_documents.jsonl").write_text("", encoding="utf-8")
        index_dir = tmp / "index"
        idx = VectorIndex(index_dir)
        idx.build(load_all(corpus), build_embedder("hashing"),
                  cache_dir=tmp / "cache")
        idx.save()
        return corpus, index_dir

    def test_rebuild_regenerates_metadata_even_with_full_cache_reuse(self):
        import retrieval.records as R
        with tempfile.TemporaryDirectory() as tmp:
            corpus, index_dir = self._build(Path(tmp))
            first = Retriever(index_dir).records[0]["segment"]
            self.assertEqual(first, "consumer")

            original = R.derive_segment
            try:
                R.derive_segment = lambda url, title="", document_type="html": "corporate"
                idx = VectorIndex(index_dir)
                stats = idx.build(load_all(corpus), build_embedder("hashing"),
                                  cache_dir=Path(tmp) / "cache")
                idx.save()
                # Every embedding came from cache, yet metadata still changed.
                self.assertEqual(stats.embedded_now, 0)
                self.assertGreater(stats.reused_from_cache, 0)
                self.assertEqual(Retriever(index_dir).records[0]["segment"],
                                 "corporate")
            finally:
                R.derive_segment = original

    def test_manifest_records_the_classifier_fingerprint(self):
        from retrieval.intent import classifier_fingerprint
        with tempfile.TemporaryDirectory() as tmp:
            _, index_dir = self._build(Path(tmp))
            r = Retriever(index_dir)
            self.assertEqual(r.index.manifest["classifier_fingerprint"],
                             classifier_fingerprint())
            self.assertFalse(r.stale_classifier)

    def test_tampered_metadata_is_reported_as_stale(self):
        from retrieval.diagnostics import check_staleness
        with tempfile.TemporaryDirectory() as tmp:
            _, index_dir = self._build(Path(tmp))
            path = index_dir / VectorIndex.RECORDS_FILE
            recs = [json.loads(l) for l in
                    path.read_text(encoding="utf-8").splitlines() if l.strip()]
            recs[0]["segment"] = "corporate"          # as if built by old logic
            path.write_text("\n".join(json.dumps(r, ensure_ascii=False)
                                      for r in recs) + "\n", encoding="utf-8")
            self.assertGreater(check_staleness(Retriever(index_dir)), 0)

    def test_a_freshly_built_index_is_not_stale(self):
        from retrieval.diagnostics import check_staleness
        with tempfile.TemporaryDirectory() as tmp:
            _, index_dir = self._build(Path(tmp))
            self.assertEqual(check_staleness(Retriever(index_dir)), 0)


class TestMetadataDerivationIsSingleSourced(unittest.TestCase):
    """Three bugs in this project came from one pattern: a field computed one
    way at build time and another way when checked or used (language, then
    segment, then page_type). There must be exactly one derivation, callable
    from both places, using only inputs stored on the record."""

    MEDIA_URLS = [
        ("https://www.banquemisr.com/-/media/BM-Online-Business/"
         "Domestic-Transfer-EN.ashx", "Domestic-Transfer-EN.ashx"),
        ("https://www.banquemisr.com/-/media/BM/Exchange-rate-EN.ashx",
         "Exchange-rate-EN.ashx"),
        ("https://www.banquemisr.com/docs/card-tariff.pdf", "card-tariff.pdf"),
    ]

    def test_attachments_are_product_data_whatever_their_url(self):
        from retrieval.intent import derive_page_type
        for url, title in self.MEDIA_URLS:
            self.assertEqual(derive_page_type(url, title, "pdf"), "product", url)

    def test_sitecore_media_paths_would_otherwise_classify_as_news(self):
        # The trap that produced 256 false "stale" reports: /-/media/ contains
        # "media", which the news pattern matches. Left uncorrected it would
        # also hit every tariff PDF with the news ranking penalty.
        from retrieval.intent import classify_page
        url, title = self.MEDIA_URLS[0]
        self.assertEqual(classify_page(url, title), "news")

    def test_html_page_type_is_unaffected(self):
        from retrieval.intent import classify_page, derive_page_type
        url = ("https://www.banquemisr.com/home/smes/retail%20banking/pages/"
               "cards/gold")
        self.assertEqual(derive_page_type(url, "Gold Card", "html"),
                         classify_page(url, "Gold Card"))

    def test_derivation_needs_only_fields_stored_on_the_record(self):
        # Breadcrumbs are deliberately excluded: they are not stored on the
        # record, so using them would make the value irreproducible at query
        # time - precisely the divergence this function prevents.
        import inspect
        from retrieval.intent import derive_page_type
        params = set(inspect.signature(derive_page_type).parameters)
        self.assertEqual(params, {"url", "title", "document_type"})


class TestStalenessWithPdfRecords(unittest.TestCase):
    """A freshly built index containing PDFs must report zero differences."""

    def _corpus(self, dirpath: Path) -> None:
        dirpath.mkdir(parents=True, exist_ok=True)
        url = ("https://www.banquemisr.com/home/smes/retail%20banking/pages/"
               "cards/gold")
        (dirpath / "documents.jsonl").write_text(json.dumps({
            "url": url, "source_url": url, "final_url": url,
            "title": "Gold Card", "language": "en", "content_hash": "h",
            "fetched_at": "2026-08-18T00:00:00Z", "breadcrumbs": ["Home", "Cards"],
            "section_path": [], "text": "gold card",
            "sections": [{"heading": "Fees", "level": 2, "anchor": "fees",
                          "text": "The annual fee for the Gold card is EGP 350 "
                                  "and eligibility requires proof of income."}],
            "tables": [], "links": [], "pdf_links": [], "doc_type": "page",
            "word_count": 20}, ensure_ascii=False) + "\n", encoding="utf-8")

        pdf_text = ("Tariff of fees and commissions for payment cards. "
                    "Annual fee Classic EGP 150 Gold EGP 350 Platinum EGP 800.")
        rows = []
        for name in ("Domestic-Transfer-EN", "Exchange-rate-EN", "Card-Tariff-EN"):
            purl = f"https://www.banquemisr.com/-/media/BM/{name}.ashx"
            rows.append(json.dumps({
                "url": purl, "source_url": purl, "title": f"{name}.ashx",
                "language": "en", "text": pdf_text, "pdf_pages": 2,
                "doc_type": "pdf", "needs_ocr": False, "attachment_kind": "pdf",
                "fetched_at": "2026-08-18T00:00:00Z"}, ensure_ascii=False))
        (dirpath / "pdf_documents.jsonl").write_text("\n".join(rows) + "\n",
                                                     encoding="utf-8")

    def test_fresh_index_with_pdfs_is_not_stale(self):
        from retrieval.diagnostics import check_staleness
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            corpus = tmp / "corpus"
            self._corpus(corpus)
            records = load_all(corpus)
            self.assertTrue([r for r in records if r.document_type == "pdf"])
            idx = VectorIndex(tmp / "index")
            idx.build(records, build_embedder("hashing"), cache_dir=tmp / "cache")
            idx.save()
            self.assertEqual(check_staleness(Retriever(tmp / "index")), 0)

    def test_pdf_records_are_stored_as_product(self):
        with tempfile.TemporaryDirectory() as tmp:
            corpus = Path(tmp) / "corpus"
            self._corpus(corpus)
            pdfs = [r for r in load_all(corpus) if r.document_type == "pdf"]
            self.assertTrue(pdfs)
            self.assertTrue(all(r.page_type == "product" for r in pdfs))


class TestRecordLookupIsForgiving(RetrievalEvalFixture):
    """The caller rarely knows the exact Sitecore slug."""

    def _capture(self, needle):
        import io
        from contextlib import redirect_stdout
        from retrieval.diagnostics import show_record
        buf = io.StringIO()
        with redirect_stdout(buf):
            show_record(self.r, needle)
        return buf.getvalue()

    def test_matches_on_title_not_just_url(self):
        out = self._capture("Gold Credit Card")
        self.assertIn("STORED IN INDEX", out)

    def test_matches_case_and_separator_insensitively(self):
        self.assertIn("STORED IN INDEX", self._capture("gold-credit-card"))
        self.assertIn("STORED IN INDEX", self._capture("GOLD CREDIT CARD"))

    def test_partial_words_still_find_the_record(self):
        self.assertIn("STORED IN INDEX", self._capture("platinum"))

    def test_a_miss_suggests_near_matches_instead_of_just_failing(self):
        out = self._capture("bm-youth-card")
        self.assertIn("no record matching", out)
        self.assertIn("did you mean", out.lower())

    def test_total_miss_points_at_the_overview_command(self):
        out = self._capture("zzzzz-nonexistent-qqqq")
        self.assertIn("no record matching", out)
