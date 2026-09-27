-- 003: Data enrichment from external sources.
--
--   * sources   gain the company they describe, which provider fetched them,
--               a stable external id (so re-runs update rather than duplicate)
--               and document metadata (annual accounts, 10-Ks, reports).
--   * facts     gain confidence, a machine-readable period, an evidence
--               location (page / XBRL tag) and notes.
--   * company_identifiers  canonical ids per register (Companies House
--               number, SEC CIK, ticker) and how each was matched.
--   * research_runs  what happened each time a provider ran, so the UI can
--               distinguish "not researched" from "credentials required"
--               from "not disclosed".


-- ---------------------------------------------------------------------------
-- Evidence types: add third-party (reported, not estimated) and AI-extracted
-- ---------------------------------------------------------------------------

ALTER TABLE facts  DROP CONSTRAINT facts_evidence_type_check;
ALTER TABLE events DROP CONSTRAINT events_evidence_type_check;

ALTER TABLE facts ADD CONSTRAINT facts_evidence_type_check CHECK (evidence_type IN (
    'verified',              -- primary source, e.g. a filed document or register
    'company_reported',      -- the company's own public statement (website)
    'third_party',           -- reported by a data vendor, e.g. Yahoo Finance
    'third_party_estimate',  -- a vendor's estimate
    'ai_extracted',          -- pulled from a primary document by an AI model
    'ai_interpretation',     -- inferred by AI
    'analyst_entry'          -- typed in manually
));
ALTER TABLE events ADD CONSTRAINT events_evidence_type_check CHECK (evidence_type IN (
    'verified', 'company_reported', 'third_party', 'third_party_estimate',
    'ai_extracted', 'ai_interpretation', 'analyst_entry'
));


-- ---------------------------------------------------------------------------
-- Sources: owner, provider, stable id, document metadata
-- ---------------------------------------------------------------------------

ALTER TABLE sources
    ADD COLUMN company_id    INT REFERENCES companies (id) ON DELETE CASCADE,
    ADD COLUMN provider      TEXT NOT NULL DEFAULT 'manual',  -- companies_house, sec, yfinance, website, manual
    ADD COLUMN external_id   TEXT,         -- e.g. CH transaction id, SEC accession number, page URL
    ADD COLUMN document_type TEXT,         -- set when the source is a document: annual_accounts, 10-K...
    ADD COLUMN period_start  DATE,         -- reporting period the document covers
    ADD COLUMN period_end    DATE,
    ADD COLUMN parse_status  TEXT CHECK (parse_status IN (
                                 'parsed',          -- facts extracted
                                 'no_document',     -- filing has no downloadable document
                                 'unreadable',      -- e.g. scanned PDF with no text layer
                                 'not_parsed',      -- readable, but no extractor for this format yet
                                 'failed'           -- download/parse error
                             )),
    ADD COLUMN notes         TEXT;

-- Existing manual sources belong to the company their facts describe.
UPDATE sources s SET company_id = f.company_id
FROM facts f WHERE f.source_id = s.id AND s.company_id IS NULL;

CREATE UNIQUE INDEX sources_provider_ref_key
    ON sources (company_id, provider, external_id) WHERE external_id IS NOT NULL;
CREATE INDEX sources_company_idx ON sources (company_id);


-- ---------------------------------------------------------------------------
-- Facts: confidence, machine-readable period, evidence location, notes
-- ---------------------------------------------------------------------------

ALTER TABLE facts
    ADD COLUMN confidence   TEXT CHECK (confidence IN ('high', 'medium', 'low')),
    ADD COLUMN period_start DATE,
    ADD COLUMN period_end   DATE,          -- end of the reporting period / balance-sheet date
    ADD COLUMN location     TEXT,          -- e.g. "page 8", "iXBRL tag uk-core:TurnoverRevenue"
    ADD COLUMN notes        TEXT;


-- ---------------------------------------------------------------------------
-- Company identifiers
-- ---------------------------------------------------------------------------

CREATE TABLE company_identifiers (
    id           SERIAL PRIMARY KEY,
    company_id   INT  NOT NULL REFERENCES companies (id) ON DELETE CASCADE,
    scheme       TEXT NOT NULL CHECK (scheme IN ('companies_house', 'sec_cik', 'ticker')),
    value        TEXT NOT NULL,
    match_method TEXT NOT NULL CHECK (match_method IN (
                     'analyst_entered',   -- typed in by you: trusted
                     'auto_exact_name',   -- a register returned exactly one exact name match
                     'derived'            -- read from another source (e.g. ticker from SEC)
                 )),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (company_id, scheme)
);


-- ---------------------------------------------------------------------------
-- Research runs
-- ---------------------------------------------------------------------------

CREATE TABLE research_runs (
    id          BIGSERIAL PRIMARY KEY,
    company_id  INT  NOT NULL REFERENCES companies (id) ON DELETE CASCADE,
    provider    TEXT NOT NULL,
    status      TEXT NOT NULL CHECK (status IN (
                    'running', 'success', 'partial', 'not_found', 'ambiguous',
                    'not_applicable', 'credentials_missing', 'rate_limited',
                    'source_unavailable', 'error'
                )),
    message     TEXT,
    detail      JSONB NOT NULL DEFAULT '{}',   -- e.g. {"not_disclosed": ["revenue"], "candidates": [...]}
    facts_added INT NOT NULL DEFAULT 0,
    started_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ
);

CREATE INDEX research_runs_lookup_idx ON research_runs (company_id, provider, started_at DESC);


-- ---------------------------------------------------------------------------
-- New fact types
-- ---------------------------------------------------------------------------

ALTER TABLE fact_types DROP CONSTRAINT fact_types_category_check;
ALTER TABLE fact_types ADD CONSTRAINT fact_types_category_check CHECK (category IN (
    'identity', 'ownership', 'governance', 'filing', 'financial', 'people',
    'product', 'market', 'strategy', 'technology'
));

INSERT INTO fact_types (code, label, category, value_kind, multi_valued, sort_order) VALUES
    ('company_number',          'Company number',                 'identity',   'text',   FALSE, 16),
    ('company_type',            'Company type',                   'identity',   'text',   FALSE, 17),
    ('incorporation_date',      'Incorporated',                   'identity',   'text',   FALSE, 18),
    ('registered_office',       'Registered office',              'identity',   'text',   FALSE, 19),
    ('sic_code',                'Industry classification (SIC)',  'identity',   'text',   TRUE,  19),
    ('exchange',                'Exchange',                       'ownership',  'text',   FALSE, 21),
    ('psc',                     'Person with significant control','governance', 'text',   TRUE,  26),
    ('officer',                 'Officer',                        'governance', 'text',   TRUE,  27),
    ('auditor',                 'Auditor',                        'governance', 'text',   FALSE, 28),
    ('gross_profit',            'Gross profit',                   'financial',  'money',  FALSE, 34),
    ('operating_profit',        'Operating profit',               'financial',  'money',  FALSE, 35),
    ('profit_before_tax',       'Profit before tax',              'financial',  'money',  FALSE, 36),
    ('net_income',              'Net profit',                     'financial',  'money',  FALSE, 37),
    ('cash',                    'Cash',                           'financial',  'money',  FALSE, 38),
    ('total_debt',              'Debt',                           'financial',  'money',  FALSE, 39),
    ('total_assets',            'Total assets',                   'financial',  'money',  FALSE, 42),
    ('total_liabilities',       'Total liabilities',              'financial',  'money',  FALSE, 43),
    ('net_assets',              'Net assets / equity',            'financial',  'money',  FALSE, 44),
    ('total_assets_less_current_liabilities',
                                'Total assets less current liabilities', 'financial', 'money', FALSE, 45),
    ('last_accounts_made_up_to','Latest accounts made up to',     'filing',     'text',   FALSE, 90),
    ('accounts_type',           'Latest accounts type',           'filing',     'text',   FALSE, 91),
    ('accounts_next_due',       'Next accounts due',              'filing',     'text',   FALSE, 92),
    ('accounting_reference_date','Accounting reference date',     'filing',     'text',   FALSE, 93),
    ('confirmation_statement_last','Last confirmation statement', 'filing',     'text',   FALSE, 94),
    ('confirmation_statement_next_due','Next confirmation statement due','filing','text', FALSE, 95),
    ('industry_served',         'Industry served',                'market',     'text',   TRUE,  63),
    ('integration',             'Integration',                    'product',    'text',   TRUE,  52);


-- ---------------------------------------------------------------------------
-- Supabase: keep the new tables off the public Data API
-- ---------------------------------------------------------------------------

ALTER TABLE company_identifiers ENABLE ROW LEVEL SECURITY;
ALTER TABLE research_runs       ENABLE ROW LEVEL SECURITY;
