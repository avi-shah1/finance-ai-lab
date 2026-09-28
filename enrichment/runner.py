"""Run every applicable provider for one company.

Order matters: registers first (they establish identity), then market
data (which may need a ticker SEC just found), then the website (which may
have been discovered by SEC/yfinance). Each provider runs in isolation:
if one fails, its research_runs row records why and the others continue.

AI extraction is a separate group, run on request: it only reads material
the structured providers already retrieved, and it costs money per call.

To add a source later (Brave, Greenhouse, GitHub, ...): write a Provider
subclass in providers/ and add it to STRUCTURED_PROVIDERS or AI_PROVIDERS.
"""

import logging

import database

from . import store
from .base import Context, EnrichmentError
from .http import Http
from .providers.ai_documents import AIDocuments
from .providers.ai_website import AIWebsite
from .providers.companies_house import CompaniesHouse
from .providers.sec import SEC
from .providers.website import Website
from .providers.yfinance_provider import YFinance

log = logging.getLogger(__name__)

STRUCTURED_PROVIDERS = [CompaniesHouse(), SEC(), YFinance(), Website()]
AI_PROVIDERS = [AIDocuments(), AIWebsite()]
GROUPS = {"structured": STRUCTURED_PROVIDERS, "ai": AI_PROVIDERS}
STRUCTURED_LABELS = {p.name: p.label for p in STRUCTURED_PROVIDERS}
AI_LABELS = {p.name: p.label for p in AI_PROVIDERS}
PROVIDER_LABELS = {**STRUCTURED_LABELS, **AI_LABELS}


def enrich_company(company_id, only=None, group="structured", providers=None, search_id=None):
    """Research one company with one group of providers (or an explicit
    list of provider objects, e.g. the lighter screening set). Returns a
    list of per-provider summaries: [{"provider", "label", "status", "message", "facts"}]."""
    company = database.get_company(company_id)
    if company is None:
        raise ValueError(f"No company with id {company_id}")
    ctx = Context(company=dict(company), identifiers=database.get_identifiers(company_id), http=Http(),
                  search_id=search_id)
    if group == "ai":
        ctx.sources = database.get_sources(company_id)

    # Providers keep per-run state (HTTP session, credentials), so each run
    # gets its own objects: runs for different companies can go in parallel.
    if providers is None:
        providers = [type(p)() for p in GROUPS[group]]

    summaries = []
    for provider in providers:
        if only and provider.name not in only:
            continue
        run_id = store.start_run(company_id, provider.name)
        status, message, detail, facts = "error", "", {}, 0
        try:
            reason = provider.not_applicable_reason(ctx)
            if reason:
                status, message = "not_applicable", reason
            else:
                result = provider.run(ctx)
                facts = store.save_result(company_id, provider.name, result)
                status, message, detail = result.status, result.message, result.detail
                # Share what we learned with the providers that run next.
                for scheme, (value, _method) in result.identifiers.items():
                    ctx.identifiers.setdefault(scheme, value)
                if result.website and not ctx.company.get("website"):
                    ctx.company["website"] = result.website
        except EnrichmentError as exc:
            status, message, detail = exc.status, str(exc), exc.detail
        except Exception as exc:  # a bug or unexpected response: record it, keep going
            log.exception("Provider %s failed for company %s", provider.name, company_id)
            status, message = "error", f"Unexpected error: {type(exc).__name__}: {exc}"
        store.finish_run(run_id, status, message, detail, facts)
        summaries.append({"provider": provider.name, "label": provider.label,
                          "status": status, "message": message, "facts": facts})
    return summaries
