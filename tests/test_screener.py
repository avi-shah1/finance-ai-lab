"""Offline tests for Level 2 screening: the rules code enforces on Claude's
assessment, the evidence lines Claude sees, news checks, and reading a UK
company number from a website. No API calls."""

import unittest
from datetime import date

from discovery.sources.base import SearchHit
from profiles import CompanyProfile
from screener import assess, evidence, news
from screener.schemas import AssessmentResult


def criterion_list(dimension, rating="strong", ids=()):
    import screening
    d = next(d for d in screening.DIMENSIONS if d["code"] == dimension)
    return [{"criterion": c, "rating": rating, "evidence_ids": list(ids), "reason": "r"} for c in d["criteria"]]


def answer(**overrides):
    dims = {}
    for code in ("strategic_fit", "commercial_attractiveness", "transaction_feasibility", "momentum_timing"):
        dims[code] = {"rating": "strong", "facts": "f", "interpretation": "i", "evidence_ids": [],
                      "criteria": criterion_list(code)}
    dims.update(overrides)
    dims.setdefault("overall", {"rating": "good_fit", "rationale": "because", "evidence_ids": ["F1"]})
    return AssessmentResult.model_validate(dims)


VALID = {"F1": ("fact", 1), "F2": ("fact", 2), "E9": ("event", 9)}


class CitationRuleTests(unittest.TestCase):
    def rows(self, result):
        rows, dropped = assess.to_rows(result, VALID)
        return {(r["dimension"], r["criterion"]): r for r in rows}, dropped

    def test_uncited_ratings_become_insufficient(self):
        rows, _ = self.rows(answer())
        self.assertEqual(rows[("strategic_fit", "product_adjacency")]["rating"], "insufficient_evidence")
        self.assertIn("No valid evidence", rows[("strategic_fit", "product_adjacency")]["rationale"])
        self.assertEqual(rows[("strategic_fit", None)]["rating"], "insufficient_evidence")
        self.assertNotIn(("overall", None), rows)      # nothing evidenced -> no overall view

    def test_invented_ids_are_removed(self):
        crit = criterion_list("strategic_fit", ids=["F1", "F999", "f2", "F1"])
        rows, dropped = self.rows(answer(strategic_fit={"rating": "strong", "facts": "f", "interpretation": "i",
                                                        "evidence_ids": ["E9"], "criteria": crit}))
        c = rows[("strategic_fit", "product_adjacency")]
        self.assertEqual((c["rating"], c["fact_ids"]), ("strong", [1, 2]))   # F999 dropped, f2 accepted, F1 once
        dim = rows[("strategic_fit", None)]
        self.assertEqual(dim["rating"], "strong")
        self.assertEqual((sorted(dim["fact_ids"]), dim["event_ids"]), ([1, 2], [9]))
        self.assertEqual(dropped, 6)                   # one bad ID in each of 6 criteria
        self.assertEqual(rows[("overall", None)]["rating"], "good_fit")

    def test_facts_and_interpretation_kept_apart(self):
        crit = criterion_list("momentum_timing", ids=["E9"])
        rows, _ = self.rows(answer(momentum_timing={"rating": "mixed", "facts": "Raised £5m in 2025.",
                                                    "interpretation": "Recent funding lowers urgency to sell.",
                                                    "evidence_ids": ["E9"], "criteria": crit}))
        dim = rows[("momentum_timing", None)]
        self.assertEqual(dim["facts_summary"], "Raised £5m in 2025.")
        self.assertEqual(dim["interpretation"], "Recent funding lowers urgency to sell.")

    def test_claude_can_say_insufficient_with_citations(self):
        crit = criterion_list("commercial_attractiveness", rating="insufficient_evidence", ids=["F1"])
        rows, _ = self.rows(answer(commercial_attractiveness={
            "rating": "insufficient_evidence", "facts": "f", "interpretation": "i", "evidence_ids": ["F1"],
            "criteria": crit}))
        self.assertEqual(rows[("commercial_attractiveness", None)]["rating"], "insufficient_evidence")
        self.assertNotIn(("overall", None), rows)


class EvidenceLineTests(unittest.TestCase):
    def fact(self, id, fact_type, value, multi=False, **kw):
        return {"id": id, "fact_type": fact_type, "label": fact_type.title(), "value_text": value,
                "multi_valued": multi, "evidence_type": kw.get("evidence_type", "company_reported"),
                "source_name": "acme.com website", "source_title": None, "excerpt": kw.get("excerpt"),
                "period": None, "period_end": None, "as_of": None, "created_at": None, "source_id": id}

    def test_lines_ids_and_filters(self):
        profile = CompanyProfile(
            {"id": 1, "name": "Acme"},
            [self.fact(10, "product", "Scheduler", multi=True, excerpt="Try Scheduler today"),
             self.fact(11, "officer", "Jane Smith", multi=True),
             self.fact(12, "employee_count", "18", evidence_type="third_party")],
            [{"id": 5, "event_type": "fundraising", "event_date": date(2025, 3, 1), "title": "Raised £5m",
              "summary": None, "evidence_type": "third_party", "source_name": "sifted.eu",
              "source_title": None, "excerpt": None, "source_id": 99}])
        lines, ids = assess.evidence_lines(profile, buyer=False)
        text = "\n".join(lines)
        self.assertIn('F10 | Product: Scheduler | Company source (acme.com website): "Try Scheduler today"', text)
        self.assertIn("F12 | Employee_Count: 18 | Third-party", text)
        self.assertIn("E5 | Fundraising, 2025-03-01: Raised £5m", text)
        self.assertNotIn("Jane Smith", text)          # officers are skipped
        self.assertEqual(ids["E5"], ("event", 5))
        buyer_lines, _ = assess.evidence_lines(profile, buyer=True)
        self.assertEqual(len(buyer_lines), 2)          # buyer: no events, only fit-related facts


class NewsTests(unittest.TestCase):
    def test_mentions(self):
        company = {"name": "Crossuite bv", "domain": "crossuite.com"}
        hit = lambda text: SearchHit(source="exa", query="q", rank=1, url="https://x.com/a", title="", text=text)
        self.assertTrue(news.mentions(company, hit("Belgian startup Crossuite raises €3m")))
        self.assertTrue(news.mentions(company, hit("see crossuite.com for details")))
        self.assertFalse(news.mentions(company, hit("Crosswise Suite launches in Paris")))
        cgm = {"name": "CompuGroup Medical SE & Co. KGaA", "domain": "cgm.com"}
        self.assertTrue(news.mentions(cgm, hit("CompuGroup Medical reports Q2 results")))
        h1 = {"name": "HealthOne (Cegedim Group 🇧🇪)", "domain": "healthone.be"}
        self.assertTrue(news.mentions(h1, hit("HealthOne launches a new GP module")))

    def test_event_dates(self):
        self.assertEqual(news._event_date("2025-03"), date(2025, 3, 1))
        self.assertEqual(news._event_date("2025-03-14"), date(2025, 3, 14))
        self.assertIsNone(news._event_date("2999-01-01"))    # future dates rejected
        self.assertIsNone(news._event_date("last spring"))


class CompanyNumberTests(unittest.TestCase):
    def test_found_with_uk_context(self):
        self.assertEqual(evidence.find_company_number(
            "© 2025 Medesk Ltd. Registered in England and Wales. Company No. 9876543"), "09876543")
        self.assertEqual(evidence.find_company_number(
            "Acme Ltd, registered in Scotland, company number SC123456"), "SC123456")

    def test_ignored_without_uk_context(self):
        self.assertIsNone(evidence.find_company_number("Handelsregister: company number 123456 (Berlin)"))
        self.assertIsNone(evidence.find_company_number("Registered in England. Call us on 0207 123456"))


if __name__ == "__main__":
    unittest.main()
