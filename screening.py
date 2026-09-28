"""The screening vocabulary: evidence types, source types, assessment
dimensions and rating labels.

The database CHECK constraints (migrations/002_evidence_model.sql) enforce
which codes are allowed; this module gives each code a human label and says
how the UI groups them. Nothing here calculates a score.
"""

# How much weight a piece of evidence can bear. Unknown information has no
# code: it is simply the absence of a fact.
EVIDENCE_TYPES = {
    "verified": "Verified filing",
    "company_reported": "Company source",
    "third_party": "Third-party",
    "third_party_estimate": "Third-party estimate",
    "ai_extracted": "AI-extracted",
    "ai_interpretation": "AI interpretation",
    "analyst_entry": "Analyst entry",
}

# The four classes the dashboard distinguishes (plus manual entries).
EVIDENCE_GROUPS = {
    "verified": "primary",
    "company_reported": "primary",
    "third_party": "third_party",
    "third_party_estimate": "third_party",
    "ai_extracted": "ai",
    "ai_interpretation": "ai",
    "analyst_entry": "analyst",
}
GROUP_LABELS = {
    "primary": "Primary / verified",
    "third_party": "Third-party",
    "ai": "AI-extracted from source",
    "analyst": "Analyst entry",
    "unknown": "Unknown",
}
GROUP_DESCRIPTIONS = {
    "primary": "Official register or filed document, or the company's own published statement.",
    "third_party": "Data vendor (e.g. Yahoo Finance). Useful, but unofficial.",
    "ai": "Extracted by an AI model from a cited primary document.",
    "analyst": "Typed in manually and not yet verified.",
    "unknown": "No evidence found, not disclosed, or not researched yet.",
}

# Precedence when sources disagree: lower wins. A weaker source never
# replaces a stronger one; both are kept and conflicts are flagged.
EVIDENCE_RANK = {
    "verified": 1,
    "company_reported": 2,
    "ai_extracted": 3,
    "analyst_entry": 4,
    "third_party": 5,
    "third_party_estimate": 6,
    "ai_interpretation": 7,
}

# Evidence types an analyst can pick in forms. AI types are only ever
# written by the (future) AI pipeline.
MANUAL_EVIDENCE_TYPES = ["analyst_entry", "verified", "company_reported", "third_party", "third_party_estimate"]

SOURCE_TYPES = {
    "manual_entry": "Manual entry",
    "regulatory_filing": "Regulatory filing",
    "annual_report": "Annual report",
    "investor_presentation": "Investor presentation",
    "company_website": "Company website",
    "press_release": "Press release",
    "news_article": "News article",
    "job_posting": "Job posting",
    "patent_record": "Patent record",
    "third_party_database": "Third-party database",
}

DOCUMENT_TYPES = {
    "annual_accounts": "Annual accounts",
    "group_accounts": "Group accounts",
    "annual_report": "Annual report",
    "annual_report_10k": "Form 10-K",
    "annual_report_20f": "Form 20-F",
    "annual_report_40f": "Form 40-F",
}

PARSE_STATUSES = {
    "parsed": "Values extracted",
    "not_parsed": "Not extracted yet",
    "unreadable": "Unreadable (scanned)",
    "no_document": "No document filed",
    "failed": "Retrieval failed",
}

# research_runs.status -> (label, tone) for the research panel
RUN_STATUSES = {
    "success": ("Complete", "good"),
    "partial": ("Partly complete", "warn"),
    "not_found": ("No match found", "muted"),
    "ambiguous": ("Needs identifier", "warn"),
    "not_applicable": ("Not applicable", "muted"),
    "credentials_missing": ("API credentials required", "bad"),
    "rate_limited": ("Rate limited", "bad"),
    "source_unavailable": ("Source unavailable", "bad"),
    "error": ("Error", "bad"),
}

IDENTIFIER_SCHEMES = {
    "companies_house": "Companies House number",
    "sec_cik": "SEC CIK",
    "ticker": "Stock ticker",
}
MATCH_METHODS = {
    "analyst_entered": "entered by you",
    "auto_exact_name": "auto-matched by exact name: please confirm",
    "derived": "from another source",
}

# How a fact was obtained (facts.extraction_method).
EXTRACTION_METHODS = {
    "manual": "Entered manually",
    "structured_api": "Structured register / API field",
    "xbrl_tag": "Value tagged by the filer (XBRL)",
    "html_rule": "Rule-based reading of a web page",
    "third_party_api": "Third-party data API",
    "llm_document": "AI-extracted from the filed document",
    "llm_website": "AI-extracted from the company web page",
}
VERIFICATIONS = {
    "quote_verified": "quote checked against source text",
    "not_verifiable": "scanned page: quote could not be checked",
}

# Rows of the financials table, in order.
FINANCIAL_ROWS = [
    "revenue", "gross_profit", "ebitda", "operating_profit", "profit_before_tax", "net_income",
    "cash", "total_debt", "creditors_due_within_one_year", "creditors_due_after_one_year",
    "total_assets", "total_liabilities", "total_assets_less_current_liabilities", "net_assets",
    "employee_count",
]

EVENT_TYPES = {
    "acquisition": "Acquisition",
    "fundraising": "Fundraising",
    "product_launch": "Product launch",
    "partnership": "Partnership",
    "geographic_expansion": "Geographic expansion",
    "hiring": "Hiring",
    "leadership_change": "Leadership change",
    "financial_results": "Financial results",
    "regulatory": "Regulatory",
    "other": "Other",
}

DIMENSION_RATINGS = {
    "very_strong": "Very Strong",
    "strong": "Strong",
    "mixed": "Mixed",
    "weak": "Weak",
    "insufficient_evidence": "Insufficient Evidence",
}

OVERALL_VIEWS = {
    "very_strong_fit": "Very Strong Fit",
    "good_fit": "Good Fit",
    "mixed": "Mixed",
    "weak_fit": "Weak Fit",
    "avoid": "Avoid",
}

# The four buyer-relative dimensions. `evidence` lists the fact types (from
# the fact_types table) and event types most relevant to each, so the UI can
# show what evidence exists even before any assessment has been made.
# Sector and geography eligibility are deliberately absent: they decide
# whether a company is a candidate at all, not how good a candidate it is.
DIMENSIONS = [
    {
        "code": "strategic_fit",
        "short": "Strategic",          # label in the results table
        "label": "Strategic fit",
        "criteria": {
            "product_adjacency": "Product adjacency",
            "customer_overlap": "Customer segment overlap",
            "capability_addition": "Capability addition",
            "geographic_relevance": "Geographic relevance",
            "strategy_alignment": "Alignment with buyer's stated strategy",
            "ma_pattern_similarity": "Similarity to historical M&A pattern",
        },
        "fact_types": ["product", "capability", "customer_segment", "geography_presence"],
        "event_types": [],
    },
    {
        "code": "commercial_attractiveness",
        "short": "Commercial",          # label in the results table
        "label": "Commercial attractiveness",
        "criteria": {
            "financial_scale": "Financial scale",
            "growth_signals": "Growth signals",
            "customer_validation": "Customer validation",
            "company_momentum": "Company momentum",
        },
        "fact_types": ["revenue", "revenue_growth", "employee_count", "notable_customer"],
        "event_types": ["financial_results"],
    },
    {
        "code": "transaction_feasibility",
        "short": "Feasibility",          # label in the results table
        "label": "Transaction feasibility",
        "criteria": {
            "relative_size": "Relative size compared with buyer",
            "ownership": "Ownership",
            "funding_history": "Funding history",
            "listing_status": "Public / private status",
            "acquisition_status": "Existing acquisition status",
        },
        "fact_types": ["ownership_status", "company_type", "exchange", "parent_company", "psc",
                       "total_funding", "last_funding_round", "investor", "legal_status"],
        "event_types": ["acquisition", "fundraising"],
    },
    {
        "code": "momentum_timing",
        "short": "Momentum",          # label in the results table
        "label": "Momentum / timing",
        "criteria": {
            "hiring": "Hiring",
            "product_launches": "Product launches",
            "fundraising": "Fundraising",
            "geographic_expansion": "Geographic expansion",
            "partnerships": "Partnerships",
            "other_developments": "Other material developments",
        },
        "fact_types": ["open_roles"],
        "event_types": ["hiring", "product_launch", "fundraising",
                        "geographic_expansion", "partnership", "leadership_change"],
    },
]

# Fact types that make up "evidence coverage" in the candidate table: how
# many of the basics we hold a sourced value for. This measures research
# completeness, not attractiveness. Values are the names shown in the checklist.
CORE_FACT_TYPES = {
    "description": "What they do",
    "ownership_status": "Ownership",
    "legal_status": "Legal status",
    "employee_count": "Employees",
    "revenue": "Revenue",
    "total_funding": "Funding raised",
    "product": "Products",
    "customer_segment": "Customer segments",
}

# Where a fact came from, in plain words for the results table.
SOURCE_SHORT_NAMES = {
    "discovery": "Exa profile",
    "companies_house": "Companies House",
    "ai_documents": "Filed accounts",
    "sec": "SEC filing",
    "yfinance": "Yahoo Finance",
    "website": "Company website",
    "ai_website": "Company website",
    "news": "News",
    "manual": "Your entry",
}
