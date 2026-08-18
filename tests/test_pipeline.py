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
        self.assertEqual(
            config.normalise_url("https://WWW.BanqueMisr.com/en/Personal/?utm_source=x#top"),
            "https://www.banquemisr.com/en/Personal",
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
