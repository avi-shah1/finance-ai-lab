"""Level 2 evidence for one candidate. Mostly free; Claude (Haiku) only
reads a few short website pages and a handful of news articles.

Order matters:
  1. company website (3 pages): its own description, links to products etc.
  2. UK company number, if the website states it (usually in the footer)
  3. registers that apply: Companies House (UK), SEC (US or listed),
     yfinance (listed). Only the latest accounts are checked; PDF accounts
     are left for Deep Research.
  4. Haiku reads the website pages for products, customers, industries...
  5. one news search for recent developments
Each step is recorded in research_runs, so a failure in one is visible on
the company page and doesn't stop the others.
"""

import logging
import re

import database
from enrichment import document_cache, store
from enrichment.base import EnrichmentError
from enrichment.providers.ai_website import AIWebsite
from enrichment.providers.companies_house import CompaniesHouse, is_uk
from enrichment.providers.sec import SEC
from enrichment.providers.website import Website
from enrichment.providers.yfinance_provider import YFinance
from enrichment.runner import enrich_company

from . import news

log = logging.getLogger(__name__)

WEBSITE_PAGES = 3
AI_PAGES = 3
AI_CHARS_PER_PAGE = 4000

US_NAMES = {"us", "usa", "united states", "united states of america"}

# "Company No. 09876543", "Registered in England and Wales, company number SC123456"
COMPANY_NUMBER = re.compile(
    r"(?:company|registration|registered)\s*(?:no\.?|number|num\.?|reg\.?\s*no\.?)\s*[:.]?\s*"
    r"((?:SC|NI|OC|SO|NC|R0)?\d{6,8})\b", re.I)
UK_CONTEXT = re.compile(r"England|Wales|Scotland|Northern Ireland|United Kingdom|\bUK\b|Companies House")


def gather(company_id, search_id=None, website_pages=WEBSITE_PAGES, ai_pages=AI_PAGES,
           ai_task="screening_extract", news_search=True):
    """Collect Level 2 evidence. Returns a list of step summaries."""
    summaries = enrich_company(company_id, providers=[Website(max_pages=website_pages)], search_id=search_id)

    derive_company_number(company_id)
    company = database.get_company(company_id)
    ids = database.get_identifiers(company_id)
    registers = []
    if ids.get("companies_house") or is_uk(company.get("hq_country")):
        registers.append(CompaniesHouse(max_accounts=1))
    if ids.get("sec_cik") or ids.get("ticker") or (company.get("hq_country") or "").strip().lower() in US_NAMES:
        registers.append(SEC())
    if registers:
        summaries += enrich_company(company_id, providers=registers, search_id=search_id)
    # SEC may have found a ticker, which is what yfinance needs.
    if database.get_identifiers(company_id).get("ticker"):
        summaries += enrich_company(company_id, providers=[YFinance()], search_id=search_id)

    summaries += enrich_company(company_id, group="ai", search_id=search_id,
                                providers=[AIWebsite(max_pages=ai_pages, max_chars=AI_CHARS_PER_PAGE, task=ai_task)])

    if news_search:
        summaries.append(_news_step(database.get_company(company_id), search_id))
    return summaries


def _news_step(company, search_id, num_results=news.NUM_RESULTS, task="screening_news"):
    run_id = store.start_run(company["id"], news.PROVIDER)
    status, message, count = "error", "", 0
    try:
        status, message, count = news.gather_news(company, search_id=search_id, num_results=num_results, task=task)
    except EnrichmentError as exc:
        status, message = exc.status, str(exc)
    except Exception as exc:
        log.exception("News failed for company %s", company["id"])
        status, message = "error", f"Unexpected error: {type(exc).__name__}: {exc}"
    store.finish_run(run_id, status, message, {}, count)
    return {"provider": news.PROVIDER, "label": "News", "status": status, "message": message, "facts": count}


def derive_company_number(company_id):
    """If the company's own website states a UK company number, record it
    (as 'derived') so Companies House looks up exactly that company rather
    than guessing by name. An identifier entered by the analyst is kept."""
    if database.get_identifiers(company_id).get("companies_house"):
        return None
    for src in database.get_sources(company_id):
        if src.get("provider") != "website" or not src.get("content_path"):
            continue
        raw = document_cache.load(src["content_path"])
        text = raw.decode("utf-8", errors="replace") if raw else ""
        number = find_company_number(text)
        if number:
            database.set_derived_identifier(company_id, "companies_house", number)
            return number
    return None


def find_company_number(text):
    if not UK_CONTEXT.search(text or ""):
        return None
    m = COMPANY_NUMBER.search(text or "")
    if not m:
        return None
    number = m.group(1).upper()
    return number.zfill(8) if number.isdigit() else number
