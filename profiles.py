"""Turn raw fact/event rows into a CompanyProfile the templates can read.

This is the one place that decides which value to show when several
sources disagree (the most recent one), and how evidence coverage is
counted. Templates stay free of that logic.
"""

from collections import defaultdict

from screening import CORE_FACT_TYPES


class CompanyProfile:
    def __init__(self, company, facts, events):
        self.company = company
        self.facts = facts
        self.events = events
        self._by_type = defaultdict(list)
        for fact in facts:
            self._by_type[fact["fact_type"]].append(fact)

    def fact(self, fact_type):
        """The current value for a fact type, or None if unknown.
        Facts arrive newest-first from the database, so the first one wins."""
        values = self._by_type.get(fact_type)
        return values[0] if values else None

    def all(self, *fact_types):
        """Every fact of the given types (for multi-valued facts and history)."""
        return [f for t in fact_types for f in self._by_type.get(t, [])]

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
    def coverage(self):
        known = sum(1 for t in CORE_FACT_TYPES if t in self._by_type)
        return {"known": known, "total": len(CORE_FACT_TYPES),
                "sources": len(self.sources)}

    @property
    def has_research(self):
        return bool(self.facts or self.events)


def build_profiles(companies, facts_by_company, events_by_company):
    return [
        CompanyProfile(c, facts_by_company.get(c["id"], []), events_by_company.get(c["id"], []))
        for c in companies
    ]
