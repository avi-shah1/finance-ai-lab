"""Turn a search (buyer, sector, geography, criteria) into web-search
queries. Claude writes descriptions of the kind of company wanted; it is
not asked for, and must not supply, company names.

Confidentiality: queries leave the app (they are sent to the search API),
so any query containing the buyer's name is dropped: a search provider
should not learn who is looking to acquire.
"""

import re

from enrichment import formatting, llm

from .schemas import QueryPlan

MAX_QUERIES = 8

SYSTEM = """You plan web searches for an M&A target-screening tool. The search engine finds company homepages by meaning, so each query should read like a description a target company might give of itself.

Rules:
- Cover different sub-segments, customer types and product types within the target sector, so results are varied.
- Always include the geography (a region or specific countries within it).
- Reflect the additional criteria when given (e.g. size, ownership, business model).
- Never include the buyer's name. Never name specific companies.
- Write each query as one plain sentence fragment, no quotes or search operators."""


def plan_queries(buyer, sector, geography, criteria=None, search_id=None):
    """Return (queries, dropped) where dropped are queries removed for
    naming the buyer."""
    content = [{"type": "text", "text": (
        f"Buyer (context only, never put this name in a query): {buyer}\n"
        f"Target sector: {sector}\n"
        f"Geography: {geography}\n"
        f"Additional criteria: {criteria or 'none'}\n\n"
        f"Write {MAX_QUERIES - 2} to {MAX_QUERIES} search queries.")}]
    plan, _meta = llm.extract(SYSTEM, content, QueryPlan, task="plan_queries", max_tokens=2000,
                              search_id=search_id)
    return clean_queries(plan.queries, buyer)


def clean_queries(queries, buyer):
    """Drop blanks, duplicates and anything naming the buyer; cap the count.
    The buyer's legal suffix is ignored: "Salesforce, Inc." matches "Salesforce"."""
    buyer_key = _key(formatting.normalise_name(buyer))
    kept, dropped, seen = [], [], set()
    for q in queries:
        q = " ".join((q or "").split())
        if not q or _key(q) in seen:
            continue
        if buyer_key and buyer_key in _key(q):
            dropped.append(q)
            continue
        seen.add(_key(q))
        kept.append(q)
    return kept[:MAX_QUERIES], dropped


def _key(text):
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())
