"""Unit tests for the data-gathering pipeline (stdlib unittest, no pytest needed).

Run: python -m unittest discover -s tests -v
"""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "data_pipeline"))

import config          # noqa: E402
import crawl           # noqa: E402
import discover        # noqa: E402
from extract import parse_page  # noqa: E402

FIXTURE = Path(__file__).parent / "fixtures" / "sample_card_page.html"
PAGE_URL = "https://www.banquemisr.com/en/personal/cards/credit-cards"


class TestUrlRules(unittest.TestCase):
    def test_normalise_drops_fragment_tracking_and_trailing_slash(self):
        # Path is lowercased: IIS/Sitecore treat paths case-insensitively.
        self.assertEqual(
            config.normalise_url("https://WWW.BanqueMisr.com/en/Personal/?utm_source=x#top"),
            "https://www.banquemisr.com/en/personal",
        )

    def test_root_path_keeps_its_slash(self):
        self.assertEqual(config.normalise_url("https://www.banquemisr.com/"),
                         "https://www.banquemisr.com/")

    def test_scope_rules(self):
        self.assertTrue(config.in_scope(PAGE_URL))
        self.assertTrue(config.in_scope("https://www.banquemisr.com/docs/tariff.pdf"))
        self.assertFalse(config.in_scope("https://facebook.com/banquemisr"))
        self.assertFalse(config.in_scope("https://www.banquemisr.com/logo.png"))
        self.assertFalse(config.in_scope("https://www.banquemisr.com/en/login"))

    def test_language_detection(self):
        self.assertEqual(config.detect_language("https://www.banquemisr.com/ar/personal"), "ar")
        self.assertEqual(config.detect_language("https://www.banquemisr.com/en/personal"), "en")
        self.assertEqual(config.detect_language("https://www.banquemisr.com/x", "ar-EG"), "ar")


class TestSitemapParsing(unittest.TestCase):
    def test_urlset(self):
        pages, nested = discover.parse_sitemap(
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            "<url><loc>https://www.banquemisr.com/en/a</loc></url></urlset>")
        self.assertEqual(pages, ["https://www.banquemisr.com/en/a"])
        self.assertEqual(nested, [])

    def test_sitemap_index_is_followed(self):
        pages, nested = discover.parse_sitemap(
            '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            "<sitemap><loc>https://www.banquemisr.com/sm1.xml</loc></sitemap></sitemapindex>")
        self.assertEqual(pages, [])
        self.assertEqual(nested, ["https://www.banquemisr.com/sm1.xml"])

    def test_malformed_xml_does_not_crash(self):
        self.assertEqual(discover.parse_sitemap("<not xml"), ([], []))


class TestLinkExtraction(unittest.TestCase):
    def test_only_in_scope_links_survive(self):
        html = ('<a href="/en/cards">c</a><a href="https://twitter.com/x">t</a>'
                '<a href="mailto:a@b.com">m</a><a href="/d/t.pdf">p</a>')
        links = crawl.extract_links(html, "https://www.banquemisr.com/en/")
        self.assertIn("https://www.banquemisr.com/en/cards", links)
        self.assertIn("https://www.banquemisr.com/d/t.pdf", links)
        self.assertEqual(len(links), 2)


class TestExtraction(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.doc = parse_page(FIXTURE.read_text(encoding="utf-8"),
                             PAGE_URL, PAGE_URL, "2026-08-17T00:00:00Z")

    def test_metadata(self):
        self.assertEqual(self.doc.title, "Credit Cards | Banque Misr")
        self.assertEqual(self.doc.language, "en")
        self.assertEqual(self.doc.section_path, ["personal", "cards", "credit-cards"])
        self.assertEqual(self.doc.breadcrumbs, ["Home", "Personal", "Credit Cards"])

    def test_boilerplate_is_stripped(self):
        self.assertNotIn("All rights reserved", self.doc.text)

    def test_sections_carry_anchors_for_citation(self):
        headings = {s.heading: s.anchor for s in self.doc.sections}
        self.assertIn("Fees and Charges", headings)
        self.assertEqual(headings["Fees and Charges"], "fees")

    def test_fee_table_is_preserved_as_structure(self):
        self.assertEqual(len(self.doc.tables), 1)
        tbl = self.doc.tables[0]
        self.assertEqual(tbl.headers, ["Card", "Annual Fee", "Minimum Limit"])
        self.assertEqual(len(tbl.rows), 3)
        self.assertIn("| Gold | EGP 350 | EGP 20,000 |", tbl.markdown)

    def test_pdf_links_are_separated_from_page_links(self):
        self.assertEqual(self.doc.pdf_links,
                         ["https://www.banquemisr.com/docs/tariff-2026.pdf"])
        self.assertNotIn("https://www.banquemisr.com/docs/tariff-2026.pdf", self.doc.links)

    def test_record_is_json_serialisable(self):
        json.dumps(self.doc.to_record(), ensure_ascii=False)


if __name__ == "__main__":
    unittest.main()


class TestVerificationGate(unittest.TestCase):
    """The trust gate must actually fail on untrustworthy corpora."""

    @staticmethod
    def _doc(**over):
        base = {"url": PAGE_URL, "title": "T", "fetched_at": "2026-08-17T00:00:00Z",
                "content_hash": "abc123", "language": "en", "word_count": 100,
                "text": "credit card eligibility fee account document "
                        "interest rate loan", "sections": [], "tables": [],
                "pdf_links": [], "links": []}
        base.update(over)
        return base

    @staticmethod
    def _manifest(url=PAGE_URL, status=200):
        return [{"url": url, "status": status, "outlinks": []}]

    def _status_of(self, checks, name):
        return next(r["status"] for r in checks.results if r["check"] == name)

    def test_empty_corpus_fails(self):
        import verify_corpus as v
        checks = v.run_checks(self._manifest(), [], [], 30)
        self.assertEqual(checks.worst(), v.FAIL)

    def test_citation_that_never_returned_2xx_fails(self):
        import verify_corpus as v
        checks = v.run_checks(self._manifest(status=404), [self._doc()], [], 30)
        self.assertEqual(
            self._status_of(checks, "every citation URL fetched successfully"), v.FAIL)

    def test_missing_provenance_fails(self):
        import verify_corpus as v
        checks = v.run_checks(self._manifest(), [self._doc(fetched_at="")], [], 30)
        self.assertEqual(
            self._status_of(checks, "every record carries provenance"), v.FAIL)

    def test_shell_page_fails(self):
        import verify_corpus as v
        checks = v.run_checks(self._manifest(), [self._doc(word_count=3)], [], 30)
        self.assertEqual(self._status_of(checks, "no empty/shell documents"), v.FAIL)

    def test_corpus_that_cannot_answer_the_brief_fails(self):
        import verify_corpus as v
        checks = v.run_checks(self._manifest(), [self._doc(text="hello world")], [], 30)
        self.assertEqual(
            self._status_of(checks, "brief's example tasks are answerable"), v.FAIL)

    def test_duplicate_content_warns(self):
        import verify_corpus as v
        d1 = self._doc()
        d2 = self._doc(url=PAGE_URL + "-copy")
        checks = v.run_checks(
            self._manifest() + [{"url": d2["url"], "status": 200, "outlinks": []}],
            [d1, d2], [], 30)
        self.assertEqual(self._status_of(checks, "no duplicate content"), v.WARN)

    def test_clean_corpus_passes(self):
        import verify_corpus as v
        doc = self._doc(
            sections=[{"heading": "Fees", "level": 2, "anchor": "fees", "text": "fee"}],
            tables=[{"caption": None, "headers": ["Card", "Annual Fee"],
                     "rows": [["Gold", "EGP 350"]], "markdown": "| Card |"}])
        checks = v.run_checks(self._manifest(), [doc], [], 30)
        self.assertEqual(checks.worst(), v.PASS)


class TestMirrorImport(unittest.TestCase):
    """URL reconstruction from a wget mirror's directory layout."""

    def test_paths_map_back_to_urls(self):
        import import_local
        from pathlib import Path
        root = Path("/m/www.banquemisr.com")
        base = "https://www.banquemisr.com"
        cases = {
            "en/personal/cards.html": "https://www.banquemisr.com/en/personal/cards",
            "en/index.html": "https://www.banquemisr.com/en",
            "index.html": "https://www.banquemisr.com/",
        }
        for rel, expected in cases.items():
            self.assertEqual(
                import_local.path_to_url(root / rel, root, base), expected, rel)


class TestAttachmentDetection(unittest.TestCase):
    """Sitecore media handlers hide documents behind .ashx and lie in headers."""

    def test_sitecore_urls_are_attachments(self):
        for url in ("https://www.banquemisr.com/-/media/BM/Exchange-rate-EN.ashx",
                    "https://www.banquemisr.com/-/media/BM/fees",
                    "https://www.banquemisr.com/docs/tariff.pdf",
                    "https://www.banquemisr.com/docs/fees.xlsx"):
            self.assertTrue(config.is_attachment_url(url), url)
        self.assertFalse(
            config.is_attachment_url("https://www.banquemisr.com/en/personal/cards"))

    def test_spreadsheets_are_no_longer_skipped(self):
        # Fee schedules are often .xlsx; skipping them loses the numbers.
        self.assertTrue(config.in_scope("https://www.banquemisr.com/docs/fees.xlsx"))
        self.assertTrue(config.in_scope("https://www.banquemisr.com/-/media/a.ashx"))

    def test_magic_bytes_beat_a_lying_content_type(self):
        # The real trap: a PDF served as text/html by the media handler.
        self.assertEqual(config.sniff_kind("text/html", b"%PDF-1.7 ..."), ("pdf", ".pdf"))
        self.assertEqual(config.sniff_kind("application/octet-stream", b"%PDF-1.4"),
                         ("pdf", ".pdf"))

    def test_scanned_image_is_identified(self):
        kind, _ = config.sniff_kind("application/octet-stream", b"\xff\xd8\xff\xe0")
        self.assertEqual(kind, "image")
        kind, _ = config.sniff_kind("image/png", b"\x89PNG\r\n")
        self.assertEqual(kind, "image")

    def test_real_html_still_reads_as_html(self):
        self.assertEqual(config.sniff_kind("text/html; charset=utf-8",
                                           b"<!DOCTYPE html>"), ("html", ".html"))


class TestRenderManifestWriteBack(unittest.TestCase):
    """render.py must update the manifest, or audit.py reports a frozen count."""

    def test_manifest_is_updated_and_flag_recomputed(self):
        import tempfile, json as _json
        from pathlib import Path as _Path
        import render

        with tempfile.TemporaryDirectory() as tmp:
            tmp = _Path(tmp)
            orig_raw, orig_manifest = config.RAW_DIR, config.CRAWL_MANIFEST
            try:
                config.RAW_DIR = tmp / "raw"
                config.CRAWL_MANIFEST = config.RAW_DIR / "manifest.jsonl"
                (config.RAW_DIR / "pages").mkdir(parents=True)

                url = "https://www.banquemisr.com/en/tariff"
                config.CRAWL_MANIFEST.write_text(_json.dumps({
                    "url": url, "final_url": url, "status": 200,
                    "content_type": "text/html", "raw_path": "old.html",
                    "looks_unrendered": True, "outlinks": [],
                }) + "\n", encoding="utf-8")

                html = ("<html><body><main><h1>Tariff</h1>"
                        + "<p>Annual fee EGP 350 eligibility interest rate.</p>" * 20
                        + '<a href="/docs/t.pdf">pdf</a></main></body></html>')
                written, updated = render.write_back(
                    {url: {"html": html, "final_url": url, "words_after": 200}})

                self.assertEqual((written, updated), (1, 1))
                rec = _json.loads(config.CRAWL_MANIFEST.read_text(encoding="utf-8"))
                self.assertTrue(rec["rendered"])
                self.assertFalse(rec["looks_unrendered"])   # recomputed, not frozen
                self.assertEqual(rec["words_after"], 200)
                self.assertTrue(rec["raw_path"].endswith(".html"))
                # link graph refreshed from the rendered DOM
                self.assertIn("https://www.banquemisr.com/docs/t.pdf", rec["outlinks"])
            finally:
                config.RAW_DIR, config.CRAWL_MANIFEST = orig_raw, orig_manifest


class TestSoft404Detection(unittest.TestCase):
    """Many unrelated URLs sharing one body = a template, not content."""

    NOT_FOUND = ("<html><body><h1>Page Not Found</h1><p>The page you requested "
                 "could not be found. Please use the navigation menu.</p></body></html>")
    REAL = ("<html><body><main><h1>Credit Cards</h1><p>Our cards include Gold "
            "and Platinum tiers with different annual fees.</p></main></body></html>")

    def test_identical_text_hashes_match_regardless_of_markup(self):
        import soft404
        a = "<html><body><p>Hello   world</p></body></html>"
        b = "<html><body><div><p>Hello world</p><script>x=1</script></div></body></html>"
        self.assertEqual(soft404.text_hash(a), soft404.text_hash(b))

    def test_distinct_pages_hash_differently(self):
        import soft404
        self.assertNotEqual(soft404.text_hash(self.REAL),
                            soft404.text_hash(self.NOT_FOUND))

    def test_not_found_wording_is_recognised(self):
        import soft404
        self.assertTrue(soft404.looks_like_not_found(self.NOT_FOUND))
        self.assertFalse(soft404.looks_like_not_found(self.REAL))

    def test_soft_404_matched_against_learned_fingerprint(self):
        import soft404
        fp = {"detected": True, "hashes": [soft404.text_hash(self.NOT_FOUND)],
              "word_counts": [20]}
        self.assertTrue(soft404.is_soft_404(self.NOT_FOUND, fp))
        self.assertFalse(soft404.is_soft_404(self.REAL, fp))

    def test_falls_back_to_wording_when_no_fingerprint_learned(self):
        import soft404
        fp = {"detected": False, "hashes": []}
        self.assertTrue(soft404.is_soft_404(self.NOT_FOUND, fp))
        self.assertFalse(soft404.is_soft_404(self.REAL, fp))

    def test_interstitials_are_named(self):
        import soft404
        cf = "<html><body>Checking your browser before accessing. Ray ID</body></html>"
        self.assertIn("cloudflare", soft404.classify_interstitial(cf))
        consent = "<html><body><div id='onetrust-banner'>We use cookies</div></body></html>"
        self.assertIn("cookie_consent", soft404.classify_interstitial(consent))
        js = "<html><body>Please enable JavaScript to continue</body></html>"
        self.assertIn("js_required", soft404.classify_interstitial(js))
        self.assertEqual(soft404.classify_interstitial(self.REAL), [])


class TestSitecoreUrlDeduplication(unittest.TestCase):
    """One page must not become dozens of documents."""

    HOME = "https://www.banquemisr.com/"

    def test_csrt_session_token_is_stripped(self):
        # Sitecore regenerates csrt per session: left in, the homepage alone
        # appears under unlimited distinct URLs.
        self.assertEqual(
            config.normalise_url(self.HOME + "?csrt=8471926352817364521"), self.HOME)
        self.assertEqual(
            config.normalise_url(self.HOME + "?csrt=99&utm_source=x"), self.HOME)

    def test_case_and_separator_variants_collapse(self):
        variants = [
            "https://WWW.BanqueMisr.com/EN/Personal",
            "https://www.banquemisr.com/en/personal",
            "https://www.banquemisr.com//en//personal/",
            "https://www.banquemisr.com/en/personal/?csrt=1",
        ]
        self.assertEqual(len({config.normalise_url(v) for v in variants}), 1)

    def test_percent_and_plus_encoding_converge(self):
        a = config.normalise_url("https://www.banquemisr.com/Home/Pages/Fees%20And%20Charges")
        b = config.normalise_url("https://www.banquemisr.com/Home/Pages/Fees+And+Charges")
        self.assertEqual(a, b)

    def test_query_param_order_does_not_create_duplicates(self):
        self.assertEqual(config.normalise_url("https://www.banquemisr.com/x?b=2&a=1"),
                         config.normalise_url("https://www.banquemisr.com/x?a=1&b=2"))

    def test_meaningful_params_are_preserved(self):
        # Only noise is dropped - a param that selects content must survive.
        self.assertIn("page=2",
                      config.normalise_url("https://www.banquemisr.com/news?page=2"))


class TestBlockPageClassification(unittest.TestCase):
    """A block page and a 404 page look alike and mean opposite things."""

    BLOCK = ("<html><body>Access Denied - Banquemisr Something went wrong This "
             "request has been temporarily blocked. This may occurs due to an "
             "invalid link or suspicious activity. For assistance, please contact "
             "our Call Center and quote your support ID: Support ID: "
             "12698372880110460572 Go Back</body></html>")
    NOT_FOUND = ("<html><body><h1>Page Not Found</h1><p>The page you requested "
                 "could not be found.</p></body></html>")
    REAL = ("<html><body><main><h1>Cards</h1><p>Gold card annual fee EGP 350 "
            "and eligibility criteria.</p></main></body></html>")

    def test_f5_block_page_is_classified_as_blocked_not_not_found(self):
        import soft404
        self.assertEqual(soft404.classify_response(self.BLOCK), "blocked")
        self.assertTrue(soft404.looks_blocked(self.BLOCK))
        # The critical distinction: never mistaken for a missing page.
        self.assertFalse(soft404.looks_like_not_found(self.BLOCK))
        self.assertIn("f5_asm_block", soft404.classify_interstitial(self.BLOCK))

    def test_not_found_is_not_treated_as_a_block(self):
        import soft404
        self.assertEqual(soft404.classify_response(self.NOT_FOUND), "not_found")
        self.assertFalse(soft404.looks_blocked(self.NOT_FOUND))

    def test_real_content_is_neither(self):
        import soft404
        self.assertEqual(soft404.classify_response(self.REAL), "content")

    def test_block_page_never_becomes_a_soft404_fingerprint(self):
        import soft404
        # A probe that was blocked tells us nothing about whether the URL
        # exists, so it must not seed the not-found fingerprint.
        fp = {"detected": False, "hashes": [], "blocked": True,
              "block_hashes": [soft404.text_hash(self.BLOCK)]}
        self.assertTrue(soft404.is_blocked_fingerprint(self.BLOCK, fp))
        self.assertFalse(soft404.is_soft_404(self.BLOCK, fp))
