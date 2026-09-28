"""The buyer's own profile, which every assessment compares targets with:
what it sells, to whom, where, and how big it is.

Sources are free registers (SEC and yfinance for listed buyers, Companies
House for UK ones) and the buyer's own website, read by Haiku. The buyer's
name is never sent to the web-search provider, so looking up the buyer
doesn't reveal the acquisition search. If the buyer's website can't be
found from a register, enter it on the search form.

A profile gathered in the last FRESH_DAYS days is reused.
"""

from datetime import datetime, timedelta, timezone

import database
from enrichment.providers.ai_website import AIWebsite
from enrichment.providers.companies_house import CompaniesHouse, is_uk
from enrichment.providers.sec import SEC
from enrichment.providers.website import Website
from enrichment.providers.yfinance_provider import YFinance
from enrichment.runner import enrich_company

FRESH_DAYS = 30
WEBSITE_PAGES = 6
AI_PAGES = 5
AI_CHARS_PER_PAGE = 5000


def ensure_buyer_profile(buyer_id, search_id=None):
    """Gather the buyer's profile unless a recent one exists. Returns a
    sentence for the search's decision log."""
    runs = database.get_latest_runs([buyer_id]).get(buyer_id, {})
    ai_run = runs.get("ai_website")
    if ai_run and ai_run["status"] in ("success", "partial") and ai_run.get("finished_at") and \
            ai_run["finished_at"] > datetime.now(timezone.utc) - timedelta(days=FRESH_DAYS):
        return f"reused the buyer profile from {ai_run['finished_at']:%d %b %Y}"

    company = database.get_company(buyer_id)
    registers = [CompaniesHouse(max_accounts=1)] if is_uk(company.get("hq_country")) or \
        database.get_identifiers(buyer_id).get("companies_house") else [SEC()]
    summaries = enrich_company(buyer_id, providers=registers, search_id=search_id)
    if database.get_identifiers(buyer_id).get("ticker"):
        summaries += enrich_company(buyer_id, providers=[YFinance()], search_id=search_id)

    if not database.get_company(buyer_id).get("website"):
        return ("buyer profile limited to registers: no website found. "
                "Add the buyer's website to profile its products and customers.")
    summaries += enrich_company(buyer_id, providers=[Website(max_pages=WEBSITE_PAGES)], search_id=search_id)
    summaries += enrich_company(buyer_id, group="ai", search_id=search_id,
                                providers=[AIWebsite(max_pages=AI_PAGES, max_chars=AI_CHARS_PER_PAGE,
                                                     task="buyer_profile")])
    ok = [s["label"] for s in summaries if s["status"] in ("success", "partial")]
    failed = [f"{s['label']} ({s['message']})" for s in summaries
              if s["status"] not in ("success", "partial", "not_applicable")]
    return ("profiled the buyer from " + (", ".join(ok) or "no sources")
            + (f"; not available: {'; '.join(failed)}" if failed else ""))
