"""Offline tests for discovery. Claude (llm.extract) and Exa (HTTP) are
replaced with canned answers, so these run without API keys or cost."""

import os
import unittest
from decimal import Decimal
from unittest import mock

from discovery import identify, planner, profile, triage
from discovery.schemas import QueryPlan, TriageItem, TriageResult
from discovery.sources.base import SearchHit
from discovery.sources.exa import ExaSource
from domains import normalise_domain

META = {"model": "claude-haiku-4-5-20251001", "input_tokens": 100, "output_tokens": 50, "request_id": "req_t"}


def hit(url, query="q1", rank=1, title=None, text=None):
    return SearchHit(source="exa", query=query, rank=rank, url=url, title=title, text=text)


class DomainTests(unittest.TestCase):
    def test_normalise(self):
        cases = {
            "https://www.Accurx.com/about?x=1": "accurx.com",
            "app.accurx.com": "accurx.com",
            "http://shop.example.co.uk/path": "example.co.uk",
            "https://www.nhs.uk": "nhs.uk",
            "accurx.com": "accurx.com",
            "": None, None: None, "not a url": None, "http://127.0.0.1/": None,
        }
        for value, expected in cases.items():
            self.assertEqual(normalise_domain(value), expected, value)


class PlannerTests(unittest.TestCase):
    def test_buyer_name_and_duplicates_are_dropped(self):
        kept, dropped = planner.clean_queries(
            ["UK patient messaging software", "uk patient  messaging software",
             "Companies like Salesforce in health", "  ", "German hospital scheduling SaaS"],
            "Salesforce, Inc.")
        self.assertEqual(kept, ["UK patient messaging software", "German hospital scheduling SaaS"])
        self.assertEqual(dropped, ["Companies like Salesforce in health"])

    @mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test"})
    def test_plan_uses_cheap_task_and_caps_count(self):
        plan = QueryPlan(queries=[f"query {i}" for i in range(12)])
        with mock.patch("enrichment.llm.extract", return_value=(plan, META)) as call:
            kept, _ = planner.plan_queries("Salesforce", "Healthcare SaaS", "Europe", search_id=5)
        self.assertEqual(len(kept), planner.MAX_QUERIES)
        self.assertEqual(call.call_args.kwargs["task"], "plan_queries")
        self.assertEqual(call.call_args.kwargs["search_id"], 5)


class IdentifyTests(unittest.TestCase):
    def test_grouping_filtering_and_order(self):
        hits = [
            hit("https://www.accurx.com/", "q1", 3, "Accurx | Healthcare communication", "short"),
            hit("https://accurx.com/products", "q2", 1, "Accurx products", "a much longer page text"),
            hit("https://www.linkedin.com/company/accurx", "q1", 1),
            hit("https://www.salesforce.com/health", "q1", 2),
            hit("https://medsys.de", "q1", 1, "MedSys GmbH – Klinik-Software"),
        ]
        candidates, rejected = identify.group_hits(hits, buyer_name="Salesforce, Inc.")
        self.assertEqual([c.domain for c in candidates], ["accurx.com", "medsys.de"])  # 2 queries beats 1
        accurx = candidates[0]
        self.assertEqual(accurx.name, "Accurx")
        self.assertEqual(accurx.best_hit.text, "a much longer page text")
        self.assertEqual({r for _, r in rejected}, {"directory, social or news site", "the buyer's own site"})
        self.assertEqual(candidates[1].name, "MedSys GmbH")

    def test_short_buyer_names_only_match_exactly(self):
        candidates, rejected = identify.group_hits(
            [hit("https://sagehealth.com"), hit("https://sage.com")], buyer_name="Sage")
        self.assertEqual([c.domain for c in candidates], ["sagehealth.com"])
        self.assertEqual(len(rejected), 1)

    def test_name_from_title_falls_back_to_domain(self):
        self.assertEqual(identify.name_from_title(None, "medsys.de"), "Medsys")


class TriageTests(unittest.TestCase):
    PAGE = "Accurx helps GP practices talk to patients. Headquartered in London, United Kingdom."

    def item(self, **overrides):
        values = dict(candidate_id="C1", company_name="Accurx", is_operating_company=True, sector_fit="yes",
                      geography_fit="yes", relevance="high",
                      summary=["- Sells patient messaging software to GP practices", " ", "Based in London, UK",
                               "based in london, uk", "A fourth bullet", "A fifth bullet"],
                      match_reason="Healthcare software used by UK GP practices.")
        values.update(overrides)
        return TriageItem(**values)

    def run_triage(self, *items):
        cand = identify.Candidate(domain="accurx.com", website="https://accurx.com", name="Accurx",
                                  hits=[hit("https://accurx.com", title="Accurx", text=self.PAGE)])
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test"}), \
             mock.patch("enrichment.llm.extract", return_value=(TriageResult(items=list(items)), META)) as call:
            verdicts = triage.triage([cand], "Healthcare SaaS", "Europe", search_id=9)
        self.assertEqual(call.call_args.kwargs["task"], "triage")
        self.assertIn(self.PAGE, call.call_args.args[1][0]["text"])
        return verdicts["accurx.com"]

    def test_in_scope(self):
        v = self.run_triage(self.item())
        self.assertTrue(v.in_scope)
        self.assertEqual((v.relevance, v.name), ("high", "Accurx"))
        # markers/blanks/duplicates removed, at most 3 bullets
        self.assertEqual(v.summary, "Sells patient messaging software to GP practices\nBased in London, UK\n"
                                    "A fourth bullet")

    def test_answers_for_unknown_ids_are_ignored(self):
        cand = identify.Candidate(domain="a.com", website="https://a.com", name="A", hits=[hit("https://a.com")])
        answer = TriageResult(items=[self.item(candidate_id="C7")])
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test"}), \
             mock.patch("enrichment.llm.extract", return_value=(answer, META)):
            self.assertEqual(triage.triage([cand], "x", "y"), {})

    def test_scope_rules(self):
        self.assertFalse(self.run_triage(self.item(geography_fit="no")).in_scope)
        self.assertTrue(self.run_triage(self.item(geography_fit="unclear")).in_scope)
        self.assertFalse(self.run_triage(self.item(is_operating_company=False)).in_scope)
        self.assertFalse(self.run_triage(self.item(sector_fit="unclear")).in_scope)
        out = self.run_triage(self.item(relevance="none"))
        self.assertFalse(out.in_scope)
        self.assertIsNone(out.relevance)


class ProfileTests(unittest.TestCase):
    CROSSUITE = ("# Crossuite bv\n\nCrossuite bv is a Hospitals and Health Care company. It offers practice "
                 "management. Crossuite bv employs 18 people (+63.6% YoY, +7 people) and has an annual revenue "
                 "of $5.5M, founded in 2007. Headquartered in Antwerpen, Antwerpen, Belgium, with presence in "
                 "Netherlands. Its workforce is distributed across Belgium and Poland.")

    def test_full_profile(self):
        p = profile.parse_profile(self.CROSSUITE)
        self.assertEqual((p.employees, p.employee_growth), (18, "+63.6% YoY, +7 people"))
        self.assertEqual((p.revenue, p.revenue_currency), (Decimal("5500000.0"), "USD"))
        self.assertEqual(p.founded, 2007)
        self.assertEqual(p.headquarters, "Antwerpen, Antwerpen, Belgium")
        self.assertIn("employs 18 people", p.employees_quote)

    def test_other_currencies_and_sizes(self):
        p = profile.parse_profile("CGM employs 4,239 people and has an annual revenue of EUR1.2B, founded in 1987. "
                                  "Headquartered in Germany, operates in 6 countries.")
        self.assertEqual((p.employees, p.revenue, p.revenue_currency), (4239, Decimal("1200000000.0"), "EUR"))
        self.assertEqual(p.headquarters, "Germany")

    def test_missing_fields_stay_empty(self):
        p = profile.parse_profile("A small clinic software maker.")
        self.assertEqual((p.employees, p.revenue, p.founded, p.headquarters), (None, None, None, None))


class ExaTests(unittest.TestCase):
    @mock.patch.dict(os.environ, {"EXA_API_KEY": "exa-test"})
    def test_request_and_parsing(self):
        http = mock.Mock()
        http.post_json.return_value = {
            "results": [{"url": "https://accurx.com", "title": "Accurx", "text": "Accurx helps GPs"},
                        {"title": "no url"}],
            "costDollars": {"total": 0.019},
        }
        with mock.patch("usage.record_service") as record:
            hits = ExaSource(http=http).search("UK GP software", num_results=12, search_id=3)
        url, payload = http.post_json.call_args.args
        self.assertEqual(payload["category"], "company")
        self.assertEqual(payload["numResults"], 12)
        self.assertEqual(http.post_json.call_args.kwargs["headers"], {"x-api-key": "exa-test"})
        self.assertEqual([(h.url, h.rank, h.text) for h in hits], [("https://accurx.com", 1, "Accurx helps GPs")])
        self.assertEqual(record.call_args.kwargs["cost_usd"], Decimal("0.019"))
        self.assertEqual(record.call_args.kwargs["search_id"], 3)

    @mock.patch.dict(os.environ, {}, clear=True)
    def test_missing_key(self):
        source = ExaSource(http=mock.Mock())
        self.assertFalse(source.available())
        with self.assertRaises(Exception) as ctx:
            source.search("x")
        self.assertIn("EXA_API_KEY", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
