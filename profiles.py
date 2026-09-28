"""Turn raw fact/event/source rows into a CompanyProfile the templates can read.

This is the one place that decides:
  * which value to show when several sources exist (precedence),
  * when two sources disagree (conflicts),
  * why something is unknown (not researched / not disclosed / credentials...),
  * how evidence coverage is counted.
Templates stay free of that logic.
"""

from collections import defaultdict
from datetime import datetime

from screening import CORE_FACT_TYPES, EVIDENCE_RANK, FINANCIAL_ROWS

# Which providers could supply a fact type, used to explain unknowns.
REGISTERS = ["companies_house", "sec"]
FINANCIAL_PROVIDERS = ["companies_house", "ai_documents", "sec", "yfinance"]
WEBSITE_PROVIDERS = ["website", "ai_website"]
FACT_PROVIDERS = {
    **{code: FINANCIAL_PROVIDERS for code in FINANCIAL_ROWS},
    "market_cap": ["yfinance"],
    "stock_ticker": ["sec", "yfinance"],
    "exchange": ["sec", "yfinance"],
    "ownership_status": ["sec"],
    "legal_name": REGISTERS, "legal_status": ["companies_house"], "company_number": ["companies_house"],
    "company_type": ["companies_house"], "incorporation_date": ["companies_house"],
    "registered_office": ["companies_house"], "sic_code": REGISTERS, "officer": ["companies_house"],
    "psc": ["companies_house"], "auditor": ["companies_house", "ai_documents"],
    "headquarters": ["sec", "website"], "description": ["website", "yfinance"],
    "product": WEBSITE_PROVIDERS, "capability": WEBSITE_PROVIDERS, "integration": WEBSITE_PROVIDERS,
    "industry_served": WEBSITE_PROVIDERS, "notable_customer": WEBSITE_PROVIDERS,
    "geography_presence": WEBSITE_PROVIDERS, "customer_segment": WEBSITE_PROVIDERS,
}

CONFLICT_TOLERANCE = 0.005     # numbers within 0.5% are treated as the same value


def _period_key(fact):
    d = fact.get("period_end") or fact.get("as_of")
    return d.toordinal() if d else 0


def _created_key(fact):
    created = fact.get("created_at")
    return created.timestamp() if isinstance(created, datetime) else 0


def precedence_key(fact):
    """Sort key: strongest evidence first, then the latest period, then the
    most recently recorded."""
    return (EVIDENCE_RANK.get(fact["evidence_type"], 9), -_period_key(fact), -_created_key(fact))


def _differs(a, b):
    if a is None or b is None:
        return False
    a, b = float(a), float(b)
    return abs(a - b) > CONFLICT_TOLERANCE * max(abs(a), abs(b), 1)


class CompanyProfile:
    def __init__(self, company, facts, events, runs=None, sources=None, identifiers=None):
        self.company = company
        self.facts = facts
        self.events = events
        self.runs = runs or {}                 # {provider: latest research_runs row}
        self.source_rows = sources or []       # full source register (company page only)
        self.identifiers = identifiers or {}   # {scheme: identifier row}
        self._by_type = defaultdict(list)
        for fact in facts:
            self._by_type[fact["fact_type"]].append(fact)
        for values in self._by_type.values():
            values.sort(key=precedence_key)

    # -- single values ------------------------------------------------------

    def fact(self, fact_type):
        """The value to display for a fact type, or None if unknown.
        Primary sources beat third-party ones even if the third-party value
        is more recent; within a tier, the latest period wins."""
        values = self._by_type.get(fact_type)
        if not values:
            return None
        best_rank = EVIDENCE_RANK.get(values[0]["evidence_type"], 9)
        tier = [v for v in values if EVIDENCE_RANK.get(v["evidence_type"], 9) == best_rank]
        return max(tier, key=lambda v: (_period_key(v), _created_key(v)))

    def all(self, *fact_types):
        """Every fact of the given types (for multi-valued facts and history)."""
        return [f for t in fact_types for f in self._by_type.get(t, [])]

    def alternatives(self, fact):
        """Other sources' values for the same fact type and period."""
        return [f for f in self._by_type.get(fact["fact_type"], [])
                if f["id"] != fact["id"] and f.get("period_end") == fact.get("period_end")]

    def conflicts(self, fact):
        """Alternatives whose number differs materially from `fact`."""
        return [f for f in self.alternatives(fact)
                if f["source_id"] != fact["source_id"] and _differs(f.get("value_number"), fact.get("value_number"))]

    # -- unknowns -----------------------------------------------------------

    def unknown_reason(self, fact_type):
        """Why a fact type has no value, in the dashboard's vocabulary."""
        if not self.runs:
            return "Not researched"
        for run in self._runs_newest_evidence_first():
            detail = run.get("detail") or {}
            if fact_type in detail.get("not_disclosed", []):
                if detail.get("not_disclosed_kind") == "pdf_only":
                    return "In PDF accounts: not extracted yet"
                return "No disclosed revenue" if fact_type == "revenue" else "Not disclosed"
        providers = FACT_PROVIDERS.get(fact_type, list(self.runs))
        relevant = [self.runs[p] for p in providers if p in self.runs]
        if not relevant:
            return "Not researched"
        statuses = {r["status"] for r in relevant}
        if statuses & {"success", "partial"}:
            return "Not available"
        if "credentials_missing" in statuses:
            return "API credentials required"
        if statuses & {"source_unavailable", "rate_limited", "error"}:
            return "Source unavailable"
        if statuses & {"ambiguous"}:
            return "Needs identifier"
        return "Not available"

    def unknown_explanation(self, fact_type):
        """Tooltip text expanding on unknown_reason()."""
        for run in self._runs_newest_evidence_first():
            detail = run.get("detail") or {}
            if fact_type in detail.get("not_disclosed", []):
                return detail.get("not_disclosed_reason")
        providers = FACT_PROVIDERS.get(fact_type, [])
        messages = [self.runs[p]["message"] for p in providers if p in self.runs and self.runs[p].get("message")]
        return " · ".join(messages) or None

    def _runs_newest_evidence_first(self):
        """An AI read of a PDF supersedes Companies House's 'PDF not
        extracted yet' note for the same filing."""
        order = ["ai_documents", "companies_house"]
        return sorted(self.runs.values(),
                      key=lambda r: order.index(r["provider"]) if r.get("provider") in order else len(order))

    # -- financials ---------------------------------------------------------

    def financial_table(self, max_periods=4):
        """{"periods": [date...], "rows": [{"code", "label", "cells": {period: cell}}]}
        where cell = {"fact": best, "conflicts": [...], "others": [...]}."""
        facts = [f for f in self.all(*FINANCIAL_ROWS) if f.get("period_end")]
        periods = sorted({f["period_end"] for f in facts}, reverse=True)[:max_periods]
        rows = []
        for code in FINANCIAL_ROWS:
            values = [f for f in self._by_type.get(code, []) if f.get("period_end") in periods]
            if not values:
                continue
            cells = {}
            for period in periods:
                in_period = [f for f in values if f["period_end"] == period]
                if in_period:
                    best = in_period[0]   # already precedence-sorted
                    cells[period] = {"fact": best, "others": in_period[1:], "conflicts": self.conflicts(best)}
            rows.append({"code": code, "label": values[0]["label"], "cells": cells})
        return {"periods": periods, "rows": rows}

    @property
    def undated_financials(self):
        """Financial facts with no period (e.g. analyst notes, market cap)."""
        return [f for f in self.all(*FINANCIAL_ROWS, "market_cap") if not f.get("period_end")]

    # -- events, sources, documents ----------------------------------------

    def events_of(self, *event_types):
        return [e for e in self.events if e["event_type"] in event_types]

    def events_except(self, *event_types):
        return [e for e in self.events if e["event_type"] not in event_types]

    @property
    def latest_event(self):
        return self.events[0] if self.events else None

    @property
    def sources(self):
        """Distinct sources behind this company's facts and events."""
        seen = {}
        for item in self.facts + self.events:
            seen.setdefault(item["source_id"], item)
        return list(seen.values())

    @property
    def documents(self):
        return [s for s in self.source_rows if s.get("document_type")]

    @property
    def coverage(self):
        # "What they do" counts whether it's the site's own words or Claude's summary.
        have = set(self._by_type) | ({"description"} if "business_summary" in self._by_type else set())
        items = [(label, code in have) for code, label in CORE_FACT_TYPES.items()]
        return {"known": sum(found for _, found in items), "total": len(items),
                "sources": len(self.sources), "items": items}

    @property
    def has_research(self):
        return bool(self.facts or self.events)

    @property
    def last_researched(self):
        times = [r["finished_at"] for r in self.runs.values() if r.get("finished_at")]
        return max(times) if times else None


def build_profiles(companies, facts_by_company, events_by_company, runs_by_company=None):
    runs_by_company = runs_by_company or {}
    return [
        CompanyProfile(c, facts_by_company.get(c["id"], []), events_by_company.get(c["id"], []),
                       runs=runs_by_company.get(c["id"], {}))
        for c in companies
    ]
