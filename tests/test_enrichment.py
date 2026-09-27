"""Offline tests for the enrichment layer. No network, no database writes.

Run:  python -m unittest discover tests
"""

import io
import os
import unittest
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest import mock

from enrichment import formatting, ixbrl, runner
from enrichment.base import (Ambiguous, Context, CredentialsMissing, NotFound, Provider,
                             ProviderResult, SourceUnavailable)
from enrichment.providers import companies_house as ch
from enrichment.providers.sec import SEC
from profiles import CompanyProfile

FIXTURE = (Path(__file__).parent / "fixtures" / "accounts.xhtml").read_bytes()


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload
        self.content = payload if isinstance(payload, bytes) else b""
        self.headers = {"Content-Type": "text/html"}

    def json(self):
        return self.payload


class FakeHttp:
    """Returns canned responses by URL (longest matching prefix wins)."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def _lookup(self, url):
        self.calls.append(url)
        matches = [k for k in self.routes if url.startswith(k)]
        if not matches:
            raise NotFound(f"no route for {url}")
        value = self.routes[max(matches, key=len)]
        if isinstance(value, Exception):
            raise value
        return value

    def get(self, url, **kwargs):
        return FakeResponse(self._lookup(url))

    def get_json(self, url, **kwargs):
        return self._lookup(url)


def blank_pdf():
    from pypdf import PdfWriter
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


# ---------------------------------------------------------------------------

class IxbrlParserTests(unittest.TestCase):
    def setUp(self):
        values = ixbrl.parse(FIXTURE, period_end=date(2025, 3, 31))
        self.by_type = {v.fact_type: v for v in values}

    def test_scaled_revenue_for_current_period_only(self):
        rev = self.by_type["revenue"]
        self.assertEqual(rev.value, Decimal("12400000"))
        self.assertEqual(rev.currency, "GBP")
        self.assertEqual((rev.start, rev.end), (date(2024, 4, 1), date(2025, 3, 31)))
        self.assertIn("Turnover", rev.excerpt)

    def test_sign_attribute_makes_value_negative(self):
        self.assertEqual(self.by_type["operating_profit"].value, Decimal("-1250000"))
        self.assertIsNone(self.by_type["operating_profit"].warning)

    def test_fixed_zero_dash_is_zero(self):
        self.assertEqual(self.by_type["cash"].value, Decimal(0))

    def test_dimensional_breakdown_ignored_and_bracket_mismatch_flagged(self):
        net = self.by_type["net_assets"]
        self.assertEqual(net.value, Decimal("3686"))     # not the 999 share-capital breakdown
        self.assertIsNotNone(net.warning)

    def test_employees_visible_occurrence_preferred_and_no_currency(self):
        emp = self.by_type["employee_count"]
        self.assertEqual(emp.value, Decimal(42))
        self.assertIsNone(emp.currency)
        self.assertIn("average number of employees", emp.excerpt)

    def test_text_concept(self):
        self.assertEqual(self.by_type["auditor"].value, "Example Audit LLP")

    def test_missing_concepts_are_absent_not_estimated(self):
        self.assertNotIn("profit_before_tax", self.by_type)
        self.assertNotIn("gross_profit", self.by_type)


class FormattingTests(unittest.TestCase):
    def test_money(self):
        self.assertEqual(formatting.money(Decimal("12400000"), "GBP"), "£12.4m")
        self.assertEqual(formatting.money(Decimal("-1250000"), "GBP"), "−£1.25m")
        self.assertEqual(formatting.money(Decimal("41525000000"), "USD"), "$41.53bn")
        self.assertEqual(formatting.money(Decimal("950"), "CHF"), "950 CHF")

    def test_period_label(self):
        self.assertEqual(formatting.period_label("2024-04-01", "2025-03-31"), "FY ended 31 Mar 2025")
        self.assertEqual(formatting.period_label(None, "2025-03-31"), "as at 31 Mar 2025")

    def test_normalise_name(self):
        self.assertEqual(formatting.normalise_name("Salesforce, Inc."), "salesforce")
        self.assertEqual(formatting.normalise_name("The Example Health Ltd"), "example health")


# ---------------------------------------------------------------------------

API, DOC = ch.API, ch.DOC_API


def ch_routes():
    return {
        f"{API}/company/01234567/officers": {"items": [
            {"name": "SMITH, Jane", "officer_role": "director", "appointed_on": "2019-05-01"},
            {"name": "DOE, John", "officer_role": "director", "resigned_on": "2021-01-01"},
        ]},
        f"{API}/company/01234567/persons-with-significant-control": {"items": [
            {"name": "Example Holdings Ltd", "kind": "corporate-entity-person-with-significant-control",
             "natures_of_control": ["ownership-of-shares-75-to-100-percent"], "notified_on": "2020-01-01"},
        ]},
        f"{API}/company/01234567/filing-history": {"items": [
            {"type": "AA", "date": "2025-12-01", "action_date": "2025-03-31", "transaction_id": "TX1",
             "description": "accounts-with-accounts-type-small",
             "links": {"document_metadata": f"{DOC}/document/D1"}},
            {"type": "AA", "date": "2024-12-01", "action_date": "2024-03-31", "transaction_id": "TX2",
             "description": "accounts-with-accounts-type-full",
             "links": {"document_metadata": f"{DOC}/document/D2"}},
            {"type": "AA", "date": "2023-12-01", "action_date": "2023-03-31", "transaction_id": "TX3",
             "description": "accounts-with-accounts-type-full", "links": {}},
            {"type": "CS01", "date": "2025-06-01", "transaction_id": "TX9"},
        ]},
        f"{API}/company/01234567": {
            "company_name": "EXAMPLE HEALTH LTD", "company_number": "01234567", "company_status": "active",
            "type": "ltd", "date_of_creation": "2012-03-14", "sic_codes": ["62012"],
            "registered_office_address": {"address_line_1": "1 Test Street", "locality": "London",
                                          "postal_code": "EC1A 1AA"},
            "accounts": {"last_accounts": {"made_up_to": "2025-03-31", "type": "small"},
                         "next_due": "2026-12-31",
                         "accounting_reference_date": {"day": "31", "month": "03"}},
            "confirmation_statement": {"last_made_up_to": "2025-06-01", "next_due": "2026-06-15"},
        },
        f"{DOC}/document/D1/content": FIXTURE,
        f"{DOC}/document/D1": {"resources": {"application/xhtml+xml": {}, "application/pdf": {}}},
        f"{DOC}/document/D2/content": blank_pdf(),
        f"{DOC}/document/D2": {"resources": {"application/pdf": {}}},
        f"{API}/search/companies": {"items": [
            {"title": "EXAMPLE HEALTH LTD", "company_number": "01234567", "company_status": "active"},
            {"title": "EXAMPLE HEALTH SERVICES LTD", "company_number": "07654321", "company_status": "active"},
        ]},
    }


class CompaniesHouseTests(unittest.TestCase):
    def ctx(self, identifiers=None, routes=None, country="UK"):
        return Context(company={"name": "Example Health", "hq_country": country, "website": None},
                       identifiers=identifiers or {}, http=FakeHttp(routes or ch_routes()))

    @mock.patch.dict(os.environ, {"COMPANIES_HOUSE_API_KEY": "test-key"})
    def test_full_run(self):
        result = ch.CompaniesHouse().run(self.ctx({"companies_house": "01234567"}))
        by_id = {s.external_id: s for s in result.sources}

        profile = {f.fact_type: f.value_text for f in by_id["profile:01234567"].facts}
        self.assertEqual(profile["legal_name"], "EXAMPLE HEALTH LTD")
        self.assertEqual(profile["incorporation_date"], "14 Mar 2012")
        self.assertEqual(profile["company_type"], "Private limited company")
        self.assertEqual(profile["accounting_reference_date"], "31 March")
        self.assertNotIn("founded_year", profile)     # incorporation != founding; not inferred

        officers = [f.value_text for f in by_id["officers:01234567"].facts]
        self.assertEqual(officers, ["Jane Smith: director, appointed 1 May 2019"])   # resigned excluded
        self.assertIn("75–100%", by_id["psc:01234567"].facts[0].value_text)

        latest = by_id["filing:TX1"]
        self.assertEqual(latest.parse_status, "parsed")
        self.assertIn("filing-history/TX1/document", latest.url)
        revenue = next(f for f in latest.facts if f.fact_type == "revenue")
        self.assertEqual((revenue.value_text, revenue.period), ("£12.4m", "FY ended 31 Mar 2025"))
        self.assertEqual((revenue.evidence_type, revenue.confidence), ("verified", "high"))
        self.assertIn("uk-core:TurnoverRevenue", revenue.location)

        self.assertEqual(by_id["filing:TX2"].parse_status, "unreadable")   # blank (scanned-like) PDF
        self.assertEqual(by_id["filing:TX3"].parse_status, "no_document")
        self.assertNotIn("filing:TX9", by_id)                              # not an accounts filing

        self.assertIn("profit_before_tax", result.detail["not_disclosed"])
        self.assertNotIn("revenue", result.detail["not_disclosed"])
        self.assertEqual(result.status, "success")

    @mock.patch.dict(os.environ, {}, clear=True)
    def test_missing_key(self):
        with self.assertRaises(CredentialsMissing):
            ch.CompaniesHouse().run(self.ctx({"companies_house": "01234567"}))

    @mock.patch.dict(os.environ, {"COMPANIES_HOUSE_API_KEY": "k"})
    def test_exact_name_match_only(self):
        result = ch.CompaniesHouse().run(self.ctx())
        self.assertEqual(result.identifiers["companies_house"], ("01234567", "auto_exact_name"))

    @mock.patch.dict(os.environ, {"COMPANIES_HOUSE_API_KEY": "k"})
    def test_ambiguous_name(self):
        routes = ch_routes()
        routes[f"{API}/search/companies"] = {"items": [
            {"title": "Example Health Limited", "company_number": "1", "company_status": "active"},
            {"title": "EXAMPLE HEALTH LTD", "company_number": "2", "company_status": "active"}]}
        with self.assertRaises(Ambiguous) as caught:
            ch.CompaniesHouse().run(self.ctx(routes=routes))
        self.assertEqual(len(caught.exception.detail["candidates"]), 2)

    @mock.patch.dict(os.environ, {"COMPANIES_HOUSE_API_KEY": "k"})
    def test_one_failing_step_gives_partial_not_crash(self):
        routes = ch_routes()
        routes[f"{API}/company/01234567/officers"] = SourceUnavailable("boom")
        result = ch.CompaniesHouse().run(self.ctx({"companies_house": "01234567"}, routes))
        self.assertEqual(result.status, "partial")
        self.assertIn("boom", result.message)

    def test_not_applicable_outside_uk(self):
        self.assertIsNotNone(ch.CompaniesHouse().not_applicable_reason(self.ctx(country="France")))


# ---------------------------------------------------------------------------

class SecTests(unittest.TestCase):
    def test_financials_use_original_filing_and_concept_priority(self):
        cik = 42
        submissions = {
            "name": "Example Corp", "tickers": ["EXM"], "exchanges": ["NASDAQ"], "sic": "7372",
            "sicDescription": "Services-Prepackaged Software", "addresses": {"business": {"city": "AUSTIN", "stateOrCountryDescription": "TX"}},
            "filings": {"recent": {
                "form": ["10-K", "10-Q", "10-K"], "accessionNumber": ["A-25", "Q-1", "A-24"],
                "filingDate": ["2025-03-01", "2024-11-01", "2024-03-01"],
                "reportDate": ["2024-12-31", "2024-09-30", "2023-12-31"],
                "primaryDocument": ["ex-2024.htm", "q.htm", "ex-2023.htm"]}},
        }
        rev = [
            # FY2023 reported in its own 10-K and again as a comparative in the FY2024 10-K
            {"start": "2023-01-01", "end": "2023-12-31", "val": 100, "accn": "A-24", "form": "10-K", "filed": "2024-03-01"},
            {"start": "2023-01-01", "end": "2023-12-31", "val": 100, "accn": "A-25", "form": "10-K", "filed": "2025-03-01"},
            {"start": "2024-01-01", "end": "2024-12-31", "val": 150, "accn": "A-25", "form": "10-K", "filed": "2025-03-01"},
            {"start": "2024-10-01", "end": "2024-12-31", "val": 40, "accn": "A-25", "form": "10-K", "filed": "2025-03-01"},  # a quarter
        ]
        companyfacts = {"facts": {"us-gaap": {
            "RevenueFromContractWithCustomerExcludingAssessedTax": {"units": {"USD": rev}},
            "Revenues": {"units": {"USD": [{"start": "2024-01-01", "end": "2024-12-31", "val": 999,
                                            "accn": "A-25", "form": "10-K", "filed": "2025-03-01"}]}},
            "Assets": {"units": {"USD": [{"end": "2024-12-31", "val": 500, "accn": "A-25", "form": "10-K", "filed": "2025-03-01"}]}},
        }}}
        http = FakeHttp({
            f"https://data.sec.gov/submissions/CIK{cik:010d}.json": submissions,
            f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json": companyfacts,
        })
        ctx = Context(company={"name": "Example Corp"}, identifiers={"sec_cik": str(cik)}, http=http)
        with mock.patch.dict(os.environ, {"SEC_USER_AGENT": "test test@example.com"}):
            result = SEC().run(ctx)

        by_id = {s.external_id: s for s in result.sources}
        fy24 = {f.fact_type: f for f in by_id["filing:A-25"].facts}
        fy23 = {f.fact_type: f for f in by_id["filing:A-24"].facts}
        self.assertEqual(fy24["revenue"].value_number, Decimal(150))       # priority concept, not 999
        self.assertEqual(fy24["revenue"].period, "FY ended 31 Dec 2024")   # annual, not the quarter
        self.assertEqual(fy23["revenue"].value_number, Decimal(100))       # attributed to its own 10-K
        self.assertEqual(fy24["total_assets"].value_number, Decimal(500))
        self.assertEqual(result.identifiers["ticker"], ("EXM", "derived"))
        self.assertNotIn("filing:Q-1", by_id)

    def test_needs_user_agent(self):
        ctx = Context(company={"name": "X"}, identifiers={"sec_cik": "1"}, http=FakeHttp({}))
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(CredentialsMissing):
                SEC().run(ctx)


# ---------------------------------------------------------------------------

def fact(id, fact_type, evidence, value, period_end=None, source_id=None, **kw):
    return {"id": id, "fact_type": fact_type, "evidence_type": evidence, "value_number": Decimal(value),
            "value_text": str(value), "period_end": period_end, "as_of": None, "source_id": source_id or id,
            "created_at": datetime(2026, 1, id, tzinfo=timezone.utc), "label": fact_type, **kw}


class ProfileTests(unittest.TestCase):
    def test_primary_beats_newer_third_party_and_conflicts_flagged(self):
        facts = [
            fact(1, "revenue", "third_party", 120, date(2025, 12, 31)),   # newer period, weaker source
            fact(2, "revenue", "verified", 100, date(2024, 12, 31)),
            fact(3, "revenue", "third_party", 130, date(2024, 12, 31)),   # disagrees with SEC
        ]
        p = CompanyProfile({"id": 1}, facts, [])
        self.assertEqual(p.fact("revenue")["id"], 2)
        self.assertEqual([c["id"] for c in p.conflicts(p.fact("revenue"))], [3])
        table = p.financial_table()
        self.assertEqual(table["rows"][0]["cells"][date(2024, 12, 31)]["fact"]["id"], 2)

    def test_small_differences_are_not_conflicts(self):
        facts = [fact(1, "revenue", "verified", 1000000, date(2024, 12, 31)),
                 fact(2, "revenue", "third_party", 1001000, date(2024, 12, 31))]
        p = CompanyProfile({"id": 1}, facts, [])
        self.assertEqual(p.conflicts(p.fact("revenue")), [])

    def test_unknown_reasons(self):
        self.assertEqual(CompanyProfile({"id": 1}, [], []).unknown_reason("revenue"), "Not researched")
        runs = {"companies_house": {"status": "success", "message": "", "detail": {
                    "not_disclosed": ["revenue"], "not_disclosed_kind": "not_tagged"}},
                "sec": {"status": "credentials_missing", "message": "", "detail": {}}}
        p = CompanyProfile({"id": 1}, [], [], runs=runs)
        self.assertEqual(p.unknown_reason("revenue"), "No disclosed revenue")
        self.assertEqual(p.unknown_reason("stock_ticker"), "API credentials required")


# ---------------------------------------------------------------------------

class RunnerTests(unittest.TestCase):
    def test_one_provider_crashing_does_not_stop_the_rest(self):
        class Boom(Provider):
            name, label = "boom", "Boom"
            def run(self, ctx):
                raise RuntimeError("unexpected")

        class Fine(Provider):
            name, label = "fine", "Fine"
            def run(self, ctx):
                return ProviderResult(message="ok")

        class Skip(Provider):
            name, label = "skip", "Skip"
            def not_applicable_reason(self, ctx):
                return "not relevant"

        finished = []
        with mock.patch.dict(runner.GROUPS, {"structured": [Boom(), Fine(), Skip()]}), \
             mock.patch.object(runner.database, "get_company", return_value={"id": 1, "name": "X"}), \
             mock.patch.object(runner.database, "get_identifiers", return_value={}), \
             mock.patch.object(runner.store, "start_run", return_value=7), \
             mock.patch.object(runner.store, "save_result", return_value=0), \
             mock.patch.object(runner.store, "finish_run", side_effect=lambda *a: finished.append(a[1])):
            summaries = runner.enrich_company(1)
        self.assertEqual([s["status"] for s in summaries], ["error", "success", "not_applicable"])
        self.assertEqual(finished, ["error", "success", "not_applicable"])


if __name__ == "__main__":
    unittest.main()
