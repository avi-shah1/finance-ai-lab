"""Offline tests for the deep research cost estimate (database mocked)."""

import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest import mock

from enrichment import document_cache
from screener import deep
from tests.test_ai_extraction import ACCOUNTS_PAGES, make_pdf


class EstimateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        patcher = mock.patch.object(document_cache, "CACHE_DIR", Path(self.tmp.name) / "cache")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)

    def run_estimate(self, company, identifiers=None, sources=(), search_id=None):
        with mock.patch.object(deep.database, "get_company", return_value=company), \
             mock.patch.object(deep.database, "get_identifiers", return_value=identifiers or {}), \
             mock.patch.object(deep.database, "get_sources", return_value=list(sources)):
            return deep.estimate(1, search_id)

    def test_non_uk_company_without_filings(self):
        est = self.run_estimate({"id": 1, "hq_country": "Germany"})
        steps = {s["label"]: s for s in est["steps"]}
        self.assertEqual(steps["AI reads filed accounts"]["high"], 0)
        self.assertNotIn("Re-assess for this search's buyer", steps)
        self.assertTrue(Decimal("0.02") < est["low"] < est["high"] < Decimal("0.15"))

    def test_uk_company_not_yet_downloaded_gives_a_range(self):
        est = self.run_estimate({"id": 1, "hq_country": "United Kingdom"}, search_id=11)
        steps = {s["label"]: s for s in est["steps"]}
        self.assertIn("Companies House", steps["Registers and website pages"]["note"])
        self.assertEqual(steps["AI reads filed accounts"]["low"], 0)
        self.assertGreater(steps["AI reads filed accounts"]["high"], 0)
        self.assertIn("Re-assess for this search's buyer", steps)

    def test_downloaded_text_pdf_is_estimated_from_its_key_pages(self):
        path, sha = document_cache.save("companies_house", make_pdf(ACCOUNTS_PAGES), ".pdf")
        src = {"document_type": "annual_accounts", "parse_status": "not_parsed", "content_path": path,
               "content_sha256": sha, "ai_extracted_sha256": None, "title": "Full accounts 2025"}
        est = self.run_estimate({"id": 1, "hq_country": "United Kingdom"}, sources=[src])
        doc = next(s for s in est["steps"] if s["label"] == "AI reads filed accounts")
        self.assertIn("text of 3 key pages", doc["note"])
        self.assertTrue(0 < doc["low"] < Decimal("0.02"))          # text mode: a cent or two

    def test_already_extracted_pdf_costs_nothing_extra(self):
        path, sha = document_cache.save("companies_house", make_pdf(ACCOUNTS_PAGES), ".pdf")
        src = {"document_type": "annual_accounts", "parse_status": "not_parsed", "content_path": path,
               "content_sha256": sha, "ai_extracted_sha256": sha, "title": "Full accounts 2025"}
        est = self.run_estimate({"id": 1, "hq_country": "United Kingdom"}, sources=[src])
        doc = next(s for s in est["steps"] if s["label"] == "AI reads filed accounts")
        self.assertEqual(doc["low"], 0)


if __name__ == "__main__":
    unittest.main()
