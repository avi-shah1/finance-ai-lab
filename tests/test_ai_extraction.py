"""Offline tests for AI extraction. The Claude call (llm.extract) is replaced
with canned answers, including deliberately wrong ones, to prove that
verification rejects anything not supported by the source.

Run:  python -m unittest discover tests
"""

import os
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest import mock

from enrichment import document_cache, llm, verify
from enrichment.ai_schemas import AccountsExtraction, Figure, TextValue, WebsiteExtraction, WebsiteItem
from enrichment.base import Context, CredentialsMissing
from enrichment.providers.ai_documents import AIDocuments
from enrichment.providers.ai_website import AIWebsite


def make_pdf(pages):
    """A minimal PDF whose pages carry real text (so pypdf can read it)."""
    objects = ["<< /Type /Catalog /Pages 2 0 R >>", None,
               "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    kids = []
    for text in pages:
        lines = " ".join(f"({line}) Tj 0 -16 Td" for line in text.split("\n"))
        stream = f"BT /F1 11 Tf 50 750 Td {lines} ET"
        objects.append(f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream")
        content_id = len(objects)
        objects.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                       f"/Resources << /Font << /F1 3 0 R >> >> /Contents {content_id} 0 R >>")
        kids.append(f"{len(objects)} 0 R")
    objects[1] = f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {len(kids)} >>"
    out, offsets = "%PDF-1.4\n", []
    for i, obj in enumerate(objects, start=1):
        offsets.append(len(out.encode("latin-1")))
        out += f"{i} 0 obj\n{obj}\nendobj\n"
    xref = len(out.encode("latin-1"))
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n"
    out += "".join(f"{o:010d} 00000 n \n" for o in offsets)
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n"
    return out.encode("latin-1")


ACCOUNTS_PAGES = [
    "EXAMPLE HEALTH LIMITED\nAnnual Report and Financial Statements\nfor the year ended 31 March 2025\nCompany number 01234567",
    "Profit and loss account for the year ended 31 March 2025\n2025 2024\nGBP'000 GBP'000\n"
    "Turnover 3 12,400 11,020\nOperating profit 1,250 980\nProfit before taxation 1,100 900",
    "Balance sheet as at 31 March 2025\nCash at bank and in hand 2,340 1,900\nNet assets 5,600 4,800\n"
    "The average number of employees during the year was 84 (2024: 71).\nAuditor: Example Audit LLP",
]


def figure(value, page, quote, multiplier=1000, currency="GBP", confidence="high"):
    return Figure(value=value, unit_multiplier=multiplier, currency=currency, page=page,
                  evidence_text=quote, confidence=confidence)


class VerifyTests(unittest.TestCase):
    def test_quote_matching_tolerates_whitespace(self):
        self.assertTrue(verify.quote_in_text("Turnover 3 12,400  11,020", "Turnover\n3\n12,400 11,020"))
        self.assertFalse(verify.quote_in_text("Turnover 3 99,999", "Turnover 3 12,400 11,020"))

    def test_number_must_be_printed_in_quote(self):
        self.assertTrue(verify.number_in_quote(12400, "Turnover 3 12,400 11,020"))
        self.assertTrue(verify.number_in_quote(-1250, "Operating loss (1,250)"))
        self.assertFalse(verify.number_in_quote(23420, "Turnover 3 12,400 11,020"))   # a derived sum


class AIDocumentsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        patcher = mock.patch.object(document_cache, "CACHE_DIR", Path(self.tmp.name) / "document_cache")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)
        path, sha = document_cache.save("companies_house", make_pdf(ACCOUNTS_PAGES), ".pdf")
        self.src = {"id": 10, "document_type": "annual_accounts", "parse_status": "not_parsed",
                    "content_path": path, "content_sha256": sha, "ai_extracted_sha256": None,
                    "title": "Full accounts made up to 31 Mar 2025", "period_end": None,
                    "published_on": None, "ai_fact_types": []}
        self.ctx = Context(company={"name": "Example Health"}, identifiers={}, http=None, sources=[self.src])

    def canned(self):
        return AccountsExtraction(
            company_name_on_document="EXAMPLE HEALTH LIMITED",
            period_start="2024-04-01", period_end="2025-03-31",
            revenue=figure(12400, 2, "Turnover 3 12,400 11,020"),
            operating_profit=figure(1250, 2, "Operating profit 1,250 980"),
            profit_before_tax=figure(1100, 3, "Profit before taxation 1,100 900"),   # wrong page -> rejected
            net_income=figure(990, 2, "Profit before taxation 1,100 900"),            # number not in quote -> rejected
            cash=figure(2340, 3, "Cash at bank and in hand 2,340 1,900"),
            total_debt=None, creditors_due_within_one_year=None, creditors_due_after_one_year=None,
            total_assets_less_current_liabilities=None,
            net_assets=figure(5600, 3, "Net assets 5,600 4,800"),
            employee_count=figure(84, 3, "The average number of employees during the year was 84 (2024: 71).",
                                  multiplier=1, currency=None),
            auditor=TextValue(value="Example Audit LLP", page=3, evidence_text="Auditor: Example Audit LLP",
                              confidence="high"),
            notes=None,
        )

    @mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test"})
    def test_verified_facts_stored_and_bad_ones_rejected(self):
        meta = {"model": "claude-opus-5", "input_tokens": 1000, "output_tokens": 200, "request_id": "req_1"}
        with mock.patch.object(llm, "extract", return_value=(self.canned(), meta)) as call:
            result = AIDocuments().run(self.ctx)

        # The PDF was sent as a base64 document block.
        content = call.call_args.args[1]
        self.assertEqual(content[0]["type"], "document")
        self.assertEqual(content[0]["source"]["media_type"], "application/pdf")

        facts = {f.fact_type: f for f in result.attach[10]["facts"]}
        rev = facts["revenue"]
        self.assertEqual(rev.value_number, Decimal("12400000"))           # 12,400 x £'000
        self.assertEqual(rev.value_text, "£12.4m")
        self.assertEqual(rev.period, "FY ended 31 Mar 2025")
        self.assertEqual((rev.evidence_type, rev.extraction_method), ("ai_extracted", "llm_document"))
        self.assertEqual((rev.evidence_page, rev.verification), (2, "quote_verified"))
        self.assertEqual(rev.extraction_model, "claude-opus-5")
        self.assertEqual(facts["cash"].period, "as at 31 Mar 2025")
        self.assertEqual(facts["employee_count"].value_text, "84 (average)")
        self.assertEqual(facts["auditor"].value_text, "Example Audit LLP")

        rejected = {r["fact_type"]: r["reason"] for r in result.detail["documents"][0]["rejected"]}
        self.assertIn("quote not found", rejected["profit_before_tax"])
        self.assertIn("number not present", rejected["net_income"])
        self.assertNotIn("profit_before_tax", facts)
        self.assertNotIn("net_income", facts)
        self.assertIn("net_income", result.detail["not_disclosed"])      # unknown, not guessed
        self.assertEqual(result.attach[10]["content_sha256"], self.src["content_sha256"])

    @mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test"})
    def test_unchanged_document_is_not_sent_again(self):
        self.src["ai_extracted_sha256"] = self.src["content_sha256"]
        with mock.patch.object(llm, "extract") as call:
            result = AIDocuments().run(self.ctx)
        call.assert_not_called()
        self.assertIn("already extracted", result.message)

    @mock.patch.dict(os.environ, {}, clear=True)
    def test_missing_key(self):
        with self.assertRaises(CredentialsMissing):
            AIDocuments().run(self.ctx)

    def test_parsed_ixbrl_documents_are_not_candidates(self):
        self.src["parse_status"] = "parsed"
        self.assertIsNotNone(AIDocuments().not_applicable_reason(self.ctx))


class AIWebsiteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        patcher = mock.patch.object(document_cache, "CACHE_DIR", Path(self.tmp.name) / "document_cache")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)
        text = ("Example Health\nOur products\nPatient Scheduling helps clinics fill appointments.\n"
                "Trusted by over 40 NHS trusts across England and Scotland.\nIntegrates with EMIS Web.")
        path, sha = document_cache.save("website", text.encode("utf-8"), ".txt")
        self.page = {"id": 21, "provider": "website", "source_type": "company_website",
                     "url": "https://example.com/products", "content_path": path,
                     "content_sha256": sha, "ai_extracted_sha256": None}
        self.ctx = Context(company={"name": "Example Health"}, identifiers={}, http=None, sources=[self.page])

    @mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test"})
    def test_only_quoted_items_from_given_pages_are_kept(self):
        url = self.page["url"]
        canned = WebsiteExtraction(items=[
            WebsiteItem(fact_type="product", value="Patient Scheduling", source_url=url,
                        evidence_text="Patient Scheduling helps clinics fill appointments.", confidence="high"),
            WebsiteItem(fact_type="customer_segment", value="NHS trusts", source_url=url,
                        evidence_text="Trusted by over 40 NHS trusts across England and Scotland.", confidence="high"),
            WebsiteItem(fact_type="integration", value="EMIS Web", source_url=url,
                        evidence_text="Integrates with EMIS Web.", confidence="high"),
            WebsiteItem(fact_type="notable_customer", value="Guy's and St Thomas'", source_url=url,
                        evidence_text="Guy's and St Thomas' uses our platform.", confidence="medium"),   # invented
            WebsiteItem(fact_type="product", value="Billing", source_url="https://example.com/other",
                        evidence_text="Billing", confidence="low"),                                      # page not given
        ])
        meta = {"model": "claude-opus-5", "input_tokens": 500, "output_tokens": 100, "request_id": "req_2"}
        with mock.patch.object(llm, "extract", return_value=(canned, meta)):
            result = AIWebsite().run(self.ctx)

        values = {(f.fact_type, f.value_text) for f in result.attach[21]["facts"]}
        self.assertEqual(values, {("product", "Patient Scheduling"), ("customer_segment", "NHS trusts"),
                                  ("integration", "EMIS Web")})
        self.assertTrue(all(f.location == url and f.extraction_method == "llm_website"
                            for f in result.attach[21]["facts"]))
        self.assertEqual(len(result.detail["rejected"]), 2)


if __name__ == "__main__":
    unittest.main()
