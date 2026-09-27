"""Shared building blocks for every data provider.

A provider turns an external source into plain Python objects:

    ProviderResult
      └── SourceDoc   (one page / filing / API response, with its URL and date)
            └── Fact  (one value that source supports, with period + evidence)

store.py then writes those objects to Postgres. Providers never touch the
database, which keeps them easy to test and easy to add.
"""

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal


@dataclass
class Fact:
    fact_type: str                     # a code from the fact_types table
    value_text: str                    # human-readable, e.g. "£12.4m"
    evidence_type: str                 # verified / company_reported / third_party / ...
    confidence: str = "high"           # high / medium / low
    value_number: Decimal | None = None
    currency: str | None = None
    period: str | None = None          # label, e.g. "FY ended 31 Mar 2025"
    period_start: date | None = None
    period_end: date | None = None
    as_of: date | None = None
    excerpt: str | None = None         # supporting text from the source
    location: str | None = None        # where in the source, e.g. "iXBRL tag uk-core:TurnoverRevenue"
    notes: str | None = None
    extraction_method: str | None = None   # defaults per provider in store.py
    extraction_model: str | None = None    # AI model id, for AI-extracted facts
    verification: str | None = None        # quote_verified / not_verifiable (AI facts)
    evidence_page: int | None = None       # 1-based page in the original document


@dataclass
class SourceDoc:
    provider: str                      # companies_house / sec / yfinance / website
    external_id: str                   # stable id within the provider, so re-runs update in place
    source_type: str                   # a code from screening.SOURCE_TYPES
    name: str                          # publisher, e.g. "Companies House"
    url: str | None = None             # clickable link for the analyst
    title: str | None = None
    published_on: date | None = None   # date of the source itself (e.g. filing date)
    document_type: str | None = None   # set when the source is a document (annual_accounts, 10-K...)
    period_start: date | None = None
    period_end: date | None = None
    parse_status: str | None = None    # parsed / no_document / unreadable / not_parsed / failed
    notes: str | None = None
    content_path: str | None = None    # cached copy (document_cache.py)
    content_sha256: str | None = None
    facts: list[Fact] = field(default_factory=list)


@dataclass
class ProviderResult:
    status: str = "success"            # success / partial / not_found (see research_runs)
    message: str = ""
    sources: list[SourceDoc] = field(default_factory=list)
    # Identifiers learned during the run: {scheme: (value, match_method)}
    identifiers: dict = field(default_factory=dict)
    website: str | None = None         # official website discovered from a source
    detail: dict = field(default_factory=dict)   # e.g. {"not_disclosed": ["revenue"]}
    # AI extractors add facts to sources that already exist:
    # {source_id: {"facts": [Fact], "content_sha256": str}}
    attach: dict = field(default_factory=dict)


@dataclass
class Context:
    """What a provider knows about the company it is researching."""
    company: dict
    identifiers: dict                  # {scheme: value}
    http: object                       # enrichment.http.Http
    sources: list = field(default_factory=list)   # existing source rows (for AI extractors)


class Provider:
    name = ""       # stored in sources.provider / research_runs.provider
    label = ""      # shown in the UI

    def not_applicable_reason(self, ctx):
        """Return a sentence explaining why this provider should be skipped,
        or None if it should run."""
        return None

    def run(self, ctx) -> ProviderResult:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Errors: each maps to a research_runs.status so the UI can explain it
# ---------------------------------------------------------------------------

class EnrichmentError(Exception):
    status = "error"

    def __init__(self, message, detail=None):
        super().__init__(message)
        self.detail = detail or {}


class CredentialsMissing(EnrichmentError):
    status = "credentials_missing"


class NotFound(EnrichmentError):
    status = "not_found"


class Ambiguous(EnrichmentError):
    status = "ambiguous"


class RateLimited(EnrichmentError):
    status = "rate_limited"


class SourceUnavailable(EnrichmentError):
    """Network failure, server error or a response we couldn't understand."""
    status = "source_unavailable"
