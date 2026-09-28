"""Level 1 discovery for one search, start to finish:

    plan queries (Claude) -> search (Exa) -> group into companies ->
    save companies + raw results -> triage (Claude) -> save candidates

Everything found is saved: raw results in discovery_hits, companies as
lightweight 'discovered' rows (reused by later searches), and every
candidate, including the ones triage filtered out, in search_candidates.
Progress is written to the search row so the page can show it while the
job runs in the background.
"""

import logging
from datetime import date
from decimal import Decimal

import database
from domains import normalise_domain
from enrichment import formatting, store
from enrichment.base import CredentialsMissing, EnrichmentError, Fact, SourceDoc

from . import identify, planner, profile, triage
from .sources.exa import ExaSource

log = logging.getLogger(__name__)

RESULTS_PER_QUERY = 12
MAX_TRIAGE = 60          # most candidates sent to triage per search (cost cap)
RELEVANCE_ORDER = {"high": 0, "medium": 1, "low": 2}


def run_discovery(search_id, source=None):
    """Discover candidates for a search. Returns a summary dict. Raises
    EnrichmentError (or a subclass) with a readable message on failure."""
    search = database.get_search(search_id)
    if search is None:
        raise ValueError(f"No search with id {search_id}")
    source = source or ExaSource()
    if not source.available():
        raise CredentialsMissing("API credentials required: add EXA_API_KEY to .env.")
    buyer = database.get_company(search["buyer_company_id"])

    database.reset_discovery(search_id)
    status = _status_writer(search_id)

    # 1. Queries
    status("Planning search queries")
    queries, dropped = planner.plan_queries(buyer["name"], search["target_sector"], search["geography"],
                                            search.get("criteria"), search_id=search_id)
    if dropped:
        database.add_search_decision(search_id, "queries", "dropped",
                                     f"{len(dropped)} quer{'y' if len(dropped) == 1 else 'ies'} named the buyer "
                                     "and were not sent to the search provider")
    if not queries:
        raise EnrichmentError("No usable search queries were produced. Try rewording the sector.")

    # 2. Search
    hits, failures = [], []
    for i, query in enumerate(queries, start=1):
        status(f"Searching {source.name.capitalize()} ({i} of {len(queries)})")
        try:
            hits.extend(source.search(query, category="company", num_results=RESULTS_PER_QUERY,
                                      search_id=search_id))
        except CredentialsMissing:
            raise
        except EnrichmentError as exc:
            failures.append(f"'{query}': {exc}")
    if failures:
        database.add_search_decision(search_id, source.name, "partial",
                                     f"{len(failures)} of {len(queries)} searches failed: {failures[0]}")
    if not hits:
        raise EnrichmentError(f"{source.name.capitalize()} returned no results"
                              + (f": {failures[0]}" if failures else "."))

    # 3. Group into companies (no AI)
    status("Identifying companies")
    candidates, rejected = identify.group_hits(hits, buyer_name=buyer["name"], buyer_domain=buyer.get("domain"))
    to_triage = candidates[:MAX_TRIAGE]
    if len(candidates) > MAX_TRIAGE:
        database.add_search_decision(search_id, "triage", "capped",
                                     f"{len(candidates)} companies found; the {MAX_TRIAGE} found by the most "
                                     "queries were checked")

    company_ids = database.upsert_discovered_companies([(c.name, c.domain, c.website) for c in to_triage])
    _save_hits(search_id, hits, company_ids)

    # 4. Triage (Claude, batches in parallel)
    status(f"Checking relevance of {len(to_triage)} companies")
    verdicts = triage.triage(to_triage, search["target_sector"], search["geography"],
                             search.get("criteria"), search_id=search_id)

    # 5. Save candidates and what the search result says about each
    status("Saving results")
    ranked = sorted(to_triage, key=lambda c: _rank_key(c, verdicts.get(c.domain)))
    rows, sources = [], []
    for rank, cand in enumerate(ranked, start=1):
        verdict = verdicts.get(cand.domain)
        company_id = company_ids[cand.domain]
        rows.append({
            "company_id": company_id, "rank": rank, "sources": cand.sources,
            "stage": "discovered" if verdict and verdict.in_scope else "filtered_out",
            "match_reason": (verdict.match_reason if verdict
                             else "Not assessed: the relevance check returned no answer for it."),
            "relevance": verdict.relevance if verdict else None,
        })
        sources.append((company_id, _profile_source(cand, verdict)))
    database.upsert_search_candidates(search_id, rows)
    try:
        store.save_sources_bulk("discovery", sources)
    except Exception:
        # Missing facts shouldn't lose the whole search: the candidates are saved.
        log.exception("Could not save discovery facts for search %s", search_id)
    relevant = sum(1 for r in rows if r["stage"] == "discovered")

    summary = {"queries": len(queries), "results": len(hits), "companies": len(candidates),
               "not_companies": len(rejected), "relevant": relevant}
    database.add_search_decision(
        search_id, source.name, "complete",
        f"{summary['results']} results from {summary['queries']} queries → {summary['companies']} companies "
        f"({summary['not_companies']} directory/news/buyer results dropped) → {relevant} relevant")
    return summary


def _status_writer(search_id):
    def write(message):
        database.set_search_status(search_id, "discovering", message)
    return write


def _rank_key(cand, verdict):
    """In-scope first, then by relevance, then by how many queries found it."""
    if verdict is None or not verdict.in_scope:
        return (1, 3, -len(cand.queries), cand.best_rank)
    return (0, RELEVANCE_ORDER.get(verdict.relevance, 3), -len(cand.queries), cand.best_rank)


def _save_hits(search_id, hits, company_ids):
    rows = []
    for h in hits:
        domain = normalise_domain(h.url)
        rows.append({"source": h.source, "purpose": "discovery", "query": h.query, "result_rank": h.rank,
                     "url": h.url, "title": (h.title or "")[:500] or None,
                     "snippet": (h.text or "")[:1000] or None, "domain": domain,
                     "company_id": company_ids.get(domain)})
    database.insert_discovery_hits(search_id, rows)


def _profile_source(cand, verdict):
    """The search result as a source, with the facts it supports:
      * employees, revenue estimate, founding year and HQ, read from Exa's
        profile text without AI: a data vendor's figures (third-party);
      * Claude's one-line summary, labelled as AI interpretation, so any
        later source (e.g. the company's own website) takes precedence."""
    hit = cand.best_hit
    p = profile.parse_profile(hit.text)
    today = date.today()
    facts = []
    if verdict:
        facts.append(Fact(fact_type="description", value_text=verdict.description,
                          evidence_type="ai_interpretation", confidence="medium",
                          excerpt=(hit.text or "").strip()[:300] or None, location=hit.url,
                          notes="AI summary of the search result text.", extraction_model=verdict.model))
    if p.employees is not None:
        facts.append(Fact(fact_type="employee_count", value_text=formatting.count(p.employees),
                          value_number=Decimal(p.employees), evidence_type="third_party", confidence="medium",
                          as_of=today, excerpt=p.employees_quote, extraction_method="third_party_api",
                          notes=f"Headcount change: {p.employee_growth}" if p.employee_growth else None))
    if p.revenue is not None:
        facts.append(Fact(fact_type="revenue", value_text=formatting.money(p.revenue, p.revenue_currency),
                          value_number=p.revenue, currency=p.revenue_currency,
                          evidence_type="third_party_estimate", confidence="low", as_of=today,
                          excerpt=p.revenue_quote, extraction_method="third_party_api",
                          notes="Annual revenue estimate from Exa's company profile, not a filed figure."))
    if p.founded is not None:
        facts.append(Fact(fact_type="founded_year", value_text=str(p.founded), value_number=Decimal(p.founded),
                          evidence_type="third_party", confidence="medium", extraction_method="third_party_api"))
    if p.headquarters:
        facts.append(Fact(fact_type="headquarters", value_text=p.headquarters, evidence_type="third_party",
                          confidence="medium", excerpt=p.headquarters_quote, extraction_method="third_party_api"))
    return SourceDoc(provider="discovery", external_id=hit.url, source_type="third_party_database",
                     name=f"Exa company profile ({cand.domain})", url=hit.url, title=hit.title,
                     notes="Company description returned by Exa search for this website.", facts=facts)
