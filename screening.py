"""The screening vocabulary: evidence types, source types, assessment
dimensions and rating labels.

The database CHECK constraints (migrations/002_evidence_model.sql) enforce
which codes are allowed; this module gives each code a human label and says
how the UI groups them. Nothing here calculates a score.
"""

# How much weight a piece of evidence can bear. Unknown information has no
# code: it is simply the absence of a fact.
EVIDENCE_TYPES = {
    "verified": "Verified",
    "company_reported": "Company-reported",
    "third_party_estimate": "Third-party estimate",
    "ai_interpretation": "AI interpretation",
    "analyst_entry": "Analyst entry",
}

# Evidence types an analyst can pick in forms. AI interpretations will only
# ever be written by the (future) analysis pipeline.
MANUAL_EVIDENCE_TYPES = ["analyst_entry", "verified", "company_reported", "third_party_estimate"]

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
        "label": "Transaction feasibility",
        "criteria": {
            "relative_size": "Relative size compared with buyer",
            "ownership": "Ownership",
            "funding_history": "Funding history",
            "listing_status": "Public / private status",
            "acquisition_status": "Existing acquisition status",
        },
        "fact_types": ["ownership_status", "parent_company", "total_funding",
                       "last_funding_round", "investor", "legal_status"],
        "event_types": ["acquisition", "fundraising"],
    },
    {
        "code": "momentum_timing",
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
# completeness, not attractiveness.
CORE_FACT_TYPES = [
    "description", "ownership_status", "legal_status", "employee_count",
    "revenue", "total_funding", "product", "customer_segment",
]
