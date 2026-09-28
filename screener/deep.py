"""Level 3: Deep Research for one company, run only when the analyst asks.

    1. registers in full (free): Companies House with 3 years of accounts,
       SEC, yfinance, wherever they apply
    2. the company website in full (free): up to 8 pages
    3. Haiku reads up to 10 website pages
    4. Haiku reads the filed PDF accounts: key-page text where the PDF has
       text (cheap), the whole PDF only for scanned filings
    5. a larger news search (10 articles)
    6. if started from a search: re-assess the company for that buyer, so
       the report reflects the deeper evidence

estimate() says what this is likely to cost before it runs; run() records
what it actually cost (from api_usage) when it finishes.
"""

import io
import logging
from datetime import datetime, timezone
from decimal import Decimal

import database
from enrichment import document_cache
from enrichment.providers.ai_documents import (DOCUMENT_TYPES, MAX_DOCUMENTS, AIDocuments, has_text_layer,
                                               key_pages, TEXT_MODE_PAGE_CHARS)
from enrichment.providers.ai_website import AIWebsite
from enrichment.providers.companies_house import CompaniesHouse, is_uk
from enrichment.providers.sec import SEC
from enrichment.providers.website import Website
from enrichment.providers.yfinance_provider import YFinance
from enrichment.runner import enrich_company

from . import assess, evidence, news
from .runner import _profile

log = logging.getLogger(__name__)

WEBSITE_PAGES = 8
AI_PAGES = 10
AI_CHARS_PER_PAGE = 12000
NEWS_RESULTS = 10

# Haiku list prices, $ per token (see usage.py); estimates only.
HAIKU_IN, HAIKU_OUT = Decimal("0.000001"), Decimal("0.000005")
CHARS_PER_TOKEN = Decimal("3.5")
PDF_PAGE_TOKENS = 2000          # a PDF page read as an image + text, roughly


def estimate(company_id, search_id=None):
    """{"steps": [{"label", "low", "high", "note"}], "low", "high"} in USD."""
    company = database.get_company(company_id)
    ids = database.get_identifiers(company_id)
    steps = []

    regs = ["Companies House (3 years of accounts)"] if (ids.get("companies_house") or is_uk(company.get("hq_country"))) else []
    if ids.get("sec_cik") or ids.get("ticker") or (company.get("hq_country") or "").strip().lower() in evidence.US_NAMES:
        regs.append("SEC")
    if ids.get("ticker"):
        regs.append("Yahoo Finance")
    steps.append({"label": "Registers and website pages", "low": Decimal(0), "high": Decimal(0),
                  "note": (", ".join(regs) + " and " if regs else "") + f"up to {WEBSITE_PAGES} website pages: free"})

    # Website pages read by Haiku: from nothing up to 10 full pages.
    web_low = _haiku(3 * 3000, 1500)
    web_high = _haiku(AI_PAGES * AI_CHARS_PER_PAGE, 5000)
    steps.append({"label": "AI reads website pages", "low": web_low, "high": web_high,
                  "note": f"up to {AI_PAGES} pages, Haiku"})

    # Filed accounts: exact for documents already downloaded, a range otherwise.
    doc_low, doc_high, notes = _documents_estimate(company_id, bool(regs and regs[0].startswith("Companies")))
    steps.append({"label": "AI reads filed accounts", "low": doc_low, "high": doc_high, "note": notes})

    steps.append({"label": "News (10 articles)", "low": Decimal("0.015"), "high": Decimal("0.035"),
                  "note": "1 Exa news search + Haiku"})
    if search_id:
        steps.append({"label": "Re-assess for this search's buyer", "low": Decimal("0.03"),
                      "high": Decimal("0.07"), "note": "Sonnet 5"})
    return {"steps": steps, "low": sum(s["low"] for s in steps), "high": sum(s["high"] for s in steps)}


def _haiku(chars_in, tokens_out):
    return (Decimal(chars_in) / CHARS_PER_TOKEN) * HAIKU_IN + Decimal(tokens_out) * HAIKU_OUT


def _documents_estimate(company_id, uk):
    docs = [s for s in database.get_sources(company_id)
            if s.get("document_type") in DOCUMENT_TYPES and s.get("parse_status") in ("not_parsed", "unreadable")
            and s.get("content_path") and s.get("ai_extracted_sha256") != s.get("content_sha256")]
    if not docs:
        if uk:
            return (Decimal(0), Decimal("0.20"),
                    "not downloaded yet: often free (machine-readable accounts), about 1-3¢ per text PDF, "
                    "up to ~10¢ per scanned PDF")
        return Decimal(0), Decimal(0), "no filed accounts available for this company"
    low = high = Decimal(0)
    parts = []
    from pypdf import PdfReader
    for src in docs[:MAX_DOCUMENTS]:
        raw = document_cache.load(src["content_path"])
        if raw is None:
            continue
        try:
            texts = [(p.extract_text() or "") for p in PdfReader(io.BytesIO(raw)).pages]
        except Exception:
            continue
        if has_text_layer(texts):
            chars = sum(min(len(texts[i]), TEXT_MODE_PAGE_CHARS) for i in key_pages(texts)) + 3000
            cost = _haiku(chars, 1500)
            parts.append(f"{src.get('title')}: text of {len(key_pages(texts))} key pages")
        else:
            pages = min(len(texts), 60)
            cost = Decimal(pages * PDF_PAGE_TOKENS) * HAIKU_IN + 1500 * HAIKU_OUT
            parts.append(f"{src.get('title')}: scanned, {pages} pages read as images")
        low, high = low + cost * Decimal("0.8"), high + cost * Decimal("1.3")
    # More filings may be downloaded during the run (up to 3 years).
    return low, high + Decimal("0.03"), "; ".join(parts) or "documents on file"


def run(company_id, search_id=None):
    """Run deep research. Returns a summary sentence; raises on failure."""
    started = datetime.now(timezone.utc)
    progress = lambda message: database.set_deep_status(company_id, "running", message)

    progress("Registers and website")
    company = database.get_company(company_id)
    ids = database.get_identifiers(company_id)
    steps = enrich_company(company_id, providers=[Website(max_pages=WEBSITE_PAGES)], search_id=search_id)
    evidence.derive_company_number(company_id)
    ids = database.get_identifiers(company_id)
    registers = []
    if ids.get("companies_house") or is_uk(company.get("hq_country")):
        registers.append(CompaniesHouse())                      # default: 3 years of accounts
    if ids.get("sec_cik") or ids.get("ticker") or (company.get("hq_country") or "").strip().lower() in evidence.US_NAMES:
        registers.append(SEC())
    if registers:
        steps += enrich_company(company_id, providers=registers, search_id=search_id)
    if database.get_identifiers(company_id).get("ticker"):
        steps += enrich_company(company_id, providers=[YFinance()], search_id=search_id)

    progress("AI reading website pages and filed accounts")
    steps += enrich_company(company_id, group="ai", search_id=search_id, providers=[
        AIWebsite(max_pages=AI_PAGES, max_chars=AI_CHARS_PER_PAGE, task="deep_website"),
        AIDocuments(mode="auto", task="deep_documents"),
    ])

    progress("News")
    steps.append(evidence._news_step(database.get_company(company_id), search_id,
                                     num_results=NEWS_RESULTS, task="deep_news"))
    database.set_research_level(company_id, "deep")

    reassessed = False
    if search_id and any(c["id"] == company_id for c in database.get_candidates(search_id)):
        progress("Re-assessing with the new evidence")
        search = database.get_search(search_id)
        assess.assess_candidate(search, _profile(search["buyer_company_id"]), company_id)
        database.mark_screened(search_id, company_id)
        reassessed = True

    cost = database.get_company_cost_since(company_id, started)
    ok = [s["label"] for s in steps if s["status"] in ("success", "partial")]
    issues = [f"{s['label']}: {s['message']}" for s in steps
              if s["status"] not in ("success", "partial", "not_applicable")]
    summary = (f"Read {len(ok)} source group(s)" + (" and re-assessed" if reassessed else "")
               + (f". Not available: {'; '.join(issues)}" if issues else "") + ".")
    database.set_deep_status(company_id, "complete", summary, cost_usd=cost)
    return summary
