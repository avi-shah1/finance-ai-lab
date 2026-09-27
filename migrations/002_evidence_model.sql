-- 002: Evidence-backed screening model.
--
-- Principles:
--   * `companies` holds identity only. Buyers and targets share this table;
--     a company's role comes from how a search references it.
--   * Every fact and event must point at a source (source_id NOT NULL).
--   * Unknown information is the absence of a row, never a placeholder value.
--   * Sector/geography decide eligibility (search_candidates); they are not
--     part of any assessment.
--
-- Allowed values are TEXT + CHECK constraints rather than Postgres ENUM types
-- because CHECK constraints are easier to change in later migrations.


-- ---------------------------------------------------------------------------
-- Companies: slim down to identity
-- ---------------------------------------------------------------------------

ALTER TABLE companies RENAME COLUMN country TO hq_country;
ALTER TABLE companies ADD COLUMN created_at TIMESTAMPTZ NOT NULL DEFAULT now();
CREATE INDEX companies_lower_name_idx ON companies (lower(name));


-- ---------------------------------------------------------------------------
-- Sources: where a piece of evidence came from
-- ---------------------------------------------------------------------------

CREATE TABLE sources (
    id           BIGSERIAL PRIMARY KEY,
    source_type  TEXT NOT NULL CHECK (source_type IN (
                     'regulatory_filing',      -- e.g. Companies House, SEC
                     'annual_report',
                     'investor_presentation',
                     'company_website',
                     'press_release',
                     'news_article',
                     'job_posting',
                     'patent_record',
                     'third_party_database',   -- e.g. Crunchbase, PitchBook
                     'manual_entry'            -- typed in by an analyst
                 )),
    name         TEXT NOT NULL,                -- publisher, e.g. "Companies House"
    title        TEXT,                         -- document/page title
    url          TEXT CHECK (url IS NULL OR url ~* '^https?://'),
    published_on DATE,                         -- date of the source itself
    retrieved_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);


-- ---------------------------------------------------------------------------
-- Fact types: the dictionary of facts we know how to store and display
-- ---------------------------------------------------------------------------

CREATE TABLE fact_types (
    code         TEXT PRIMARY KEY,
    label        TEXT NOT NULL,
    category     TEXT NOT NULL CHECK (category IN (
                     'identity', 'ownership', 'financial', 'people',
                     'product', 'market', 'strategy', 'technology'
                 )),
    value_kind   TEXT NOT NULL CHECK (value_kind IN ('text', 'number', 'money')),
    multi_valued BOOLEAN NOT NULL DEFAULT FALSE,  -- e.g. many products per company
    sort_order   INT NOT NULL DEFAULT 100
);

INSERT INTO fact_types (code, label, category, value_kind, multi_valued, sort_order) VALUES
    ('description',          'Description',            'identity',   'text',   FALSE, 10),
    ('legal_name',           'Legal name',             'identity',   'text',   FALSE, 11),
    ('registration_number',  'Registration number',    'identity',   'text',   FALSE, 12),
    ('legal_status',         'Legal status',           'identity',   'text',   FALSE, 13),
    ('founded_year',         'Founded',                'identity',   'number', FALSE, 14),
    ('headquarters',         'Headquarters',           'identity',   'text',   FALSE, 15),
    ('ownership_status',     'Ownership',              'ownership',  'text',   FALSE, 20),
    ('stock_ticker',         'Stock ticker',           'ownership',  'text',   FALSE, 21),
    ('parent_company',       'Parent company',         'ownership',  'text',   FALSE, 22),
    ('total_funding',        'Total funding raised',   'ownership',  'money',  FALSE, 23),
    ('last_funding_round',   'Last funding round',     'ownership',  'text',   FALSE, 24),
    ('investor',             'Investor',               'ownership',  'text',   TRUE,  25),
    ('revenue',              'Revenue',                'financial',  'money',  FALSE, 30),
    ('revenue_growth',       'Revenue growth',         'financial',  'text',   FALSE, 31),
    ('ebitda',               'EBITDA',                 'financial',  'money',  FALSE, 32),
    ('market_cap',           'Market capitalisation',  'financial',  'money',  FALSE, 33),
    ('employee_count',       'Employees',              'people',     'number', FALSE, 40),
    ('open_roles',           'Open roles',             'people',     'number', FALSE, 41),
    ('product',              'Product',                'product',    'text',   TRUE,  50),
    ('capability',           'Capability',             'product',    'text',   TRUE,  51),
    ('customer_segment',     'Customer segment',       'market',     'text',   TRUE,  60),
    ('geography_presence',   'Geographic presence',    'market',     'text',   TRUE,  61),
    ('notable_customer',     'Notable customer',       'market',     'text',   TRUE,  62),
    ('strategic_priority',   'Strategic priority',     'strategy',   'text',   TRUE,  70),
    ('patent_count',         'Patents',                'technology', 'number', FALSE, 80),
    ('technology',           'Technology',             'technology', 'text',   TRUE,  81);


-- ---------------------------------------------------------------------------
-- Facts: one sourced claim about one company
-- ---------------------------------------------------------------------------

CREATE TABLE facts (
    id            BIGSERIAL PRIMARY KEY,
    company_id    INT    NOT NULL REFERENCES companies (id) ON DELETE CASCADE,
    fact_type     TEXT   NOT NULL REFERENCES fact_types (code),
    value_text    TEXT   NOT NULL,             -- as displayed/quoted, e.g. "£12m"
    value_number  NUMERIC,                     -- machine-readable, only when certain
    currency      CHAR(3),                     -- ISO code for money values, e.g. GBP
    period        TEXT,                        -- e.g. "FY2024"
    as_of         DATE,                        -- date the value refers to
    source_id     BIGINT NOT NULL REFERENCES sources (id) ON DELETE RESTRICT,
    evidence_type TEXT   NOT NULL CHECK (evidence_type IN (
                      'verified',              -- confirmed in an authoritative source
                      'company_reported',      -- the company's own claim
                      'third_party_estimate',  -- e.g. data-vendor estimate
                      'ai_interpretation',     -- inferred by AI from sources
                      'analyst_entry'          -- entered manually, not yet verified
                  )),
    excerpt       TEXT,                        -- supporting quote from the source
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX facts_company_type_idx ON facts (company_id, fact_type);
CREATE INDEX facts_source_idx ON facts (source_id);


-- ---------------------------------------------------------------------------
-- Events: dated developments (news, deals, launches, hiring pushes...)
-- ---------------------------------------------------------------------------

CREATE TABLE events (
    id                 BIGSERIAL PRIMARY KEY,
    company_id         INT    NOT NULL REFERENCES companies (id) ON DELETE CASCADE,
    related_company_id INT    REFERENCES companies (id) ON DELETE SET NULL,  -- e.g. the company acquired
    event_type         TEXT   NOT NULL CHECK (event_type IN (
                           'acquisition', 'fundraising', 'product_launch', 'partnership',
                           'geographic_expansion', 'hiring', 'leadership_change',
                           'financial_results', 'regulatory', 'other'
                       )),
    title              TEXT   NOT NULL,
    summary            TEXT,
    event_date         DATE,
    source_id          BIGINT NOT NULL REFERENCES sources (id) ON DELETE RESTRICT,
    evidence_type      TEXT   NOT NULL CHECK (evidence_type IN (
                           'verified', 'company_reported', 'third_party_estimate',
                           'ai_interpretation', 'analyst_entry'
                       )),
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX events_company_date_idx ON events (company_id, event_date DESC);
CREATE INDEX events_source_idx ON events (source_id);


-- ---------------------------------------------------------------------------
-- Searches and the candidate universe (eligibility, not scoring)
-- ---------------------------------------------------------------------------

CREATE TABLE searches (
    id               SERIAL PRIMARY KEY,
    buyer_company_id INT  NOT NULL REFERENCES companies (id) ON DELETE RESTRICT,
    target_sector    TEXT NOT NULL,
    geography        TEXT NOT NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE search_candidates (
    search_id  INT  NOT NULL REFERENCES searches (id) ON DELETE CASCADE,
    company_id INT  NOT NULL REFERENCES companies (id) ON DELETE CASCADE,
    origin     TEXT NOT NULL DEFAULT 'manual' CHECK (origin IN ('manual', 'discovery')),
    added_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (search_id, company_id)
);


-- ---------------------------------------------------------------------------
-- Assessments: qualitative, buyer-relative ratings (none are created yet)
-- ---------------------------------------------------------------------------
-- A row with criterion NULL is the rating for the whole dimension.
-- dimension = 'overall' holds the final screening view.
-- Rows are append-only: the newest row per (search, company, dimension,
-- criterion) is the current view, older rows are history.

CREATE TABLE assessments (
    id          BIGSERIAL PRIMARY KEY,
    search_id   INT  NOT NULL,
    company_id  INT  NOT NULL,
    dimension   TEXT NOT NULL CHECK (dimension IN (
                    'strategic_fit', 'commercial_attractiveness',
                    'transaction_feasibility', 'momentum_timing', 'overall'
                )),
    criterion   TEXT,                           -- e.g. 'product_adjacency'
    rating      TEXT NOT NULL,
    rationale   TEXT,
    assessed_by TEXT NOT NULL CHECK (assessed_by IN ('analyst', 'ai')),
    model       TEXT,                           -- AI model id, when assessed_by = 'ai'
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (search_id, company_id)
        REFERENCES search_candidates (search_id, company_id) ON DELETE CASCADE,
    CHECK (
        (dimension = 'overall' AND criterion IS NULL AND rating IN (
            'very_strong_fit', 'good_fit', 'mixed', 'weak_fit', 'avoid'))
        OR
        (dimension <> 'overall' AND rating IN (
            'very_strong', 'strong', 'mixed', 'weak', 'insufficient_evidence'))
    )
);

CREATE INDEX assessments_lookup_idx ON assessments (search_id, company_id, dimension, criterion, created_at DESC);

CREATE TABLE assessment_evidence (
    id            BIGSERIAL PRIMARY KEY,
    assessment_id BIGINT NOT NULL REFERENCES assessments (id) ON DELETE CASCADE,
    fact_id       BIGINT REFERENCES facts (id) ON DELETE CASCADE,
    event_id      BIGINT REFERENCES events (id) ON DELETE CASCADE,
    CHECK (num_nonnulls(fact_id, event_id) = 1)
);


-- ---------------------------------------------------------------------------
-- Move the original free-text columns into sourced facts
-- ---------------------------------------------------------------------------
-- Values are kept exactly as typed. value_number is only filled where the
-- text is a plain whole number (e.g. employees "50"); "10M" has no known
-- currency so it stays text only.

WITH legacy AS (
    SELECT id, nullif(trim(revenue), '') AS revenue,
               nullif(trim(employees), '') AS employees,
               nullif(trim(description), '') AS description
    FROM companies
),
src AS (
    INSERT INTO sources (source_type, name, title)
    SELECT 'manual_entry', 'Analyst entry', 'Entered via the original company form'
    WHERE EXISTS (SELECT 1 FROM legacy
                  WHERE coalesce(revenue, employees, description) IS NOT NULL)
    RETURNING id
)
INSERT INTO facts (company_id, fact_type, value_text, value_number, source_id, evidence_type)
SELECT l.id, 'revenue', l.revenue, NULL, src.id, 'analyst_entry'
    FROM legacy l, src WHERE l.revenue IS NOT NULL
UNION ALL
SELECT l.id, 'employee_count', l.employees,
       CASE WHEN l.employees ~ '^[0-9]+$' THEN l.employees::numeric END,
       src.id, 'analyst_entry'
    FROM legacy l, src WHERE l.employees IS NOT NULL
UNION ALL
SELECT l.id, 'description', l.description, NULL, src.id, 'analyst_entry'
    FROM legacy l, src WHERE l.description IS NOT NULL;

ALTER TABLE companies DROP COLUMN revenue;
ALTER TABLE companies DROP COLUMN employees;
ALTER TABLE companies DROP COLUMN description;


-- ---------------------------------------------------------------------------
-- Supabase: block the public Data API from these tables.
-- The Flask app connects as the table owner, which bypasses RLS.
-- ---------------------------------------------------------------------------

ALTER TABLE companies           ENABLE ROW LEVEL SECURITY;
ALTER TABLE sources             ENABLE ROW LEVEL SECURITY;
ALTER TABLE fact_types          ENABLE ROW LEVEL SECURITY;
ALTER TABLE facts               ENABLE ROW LEVEL SECURITY;
ALTER TABLE events              ENABLE ROW LEVEL SECURITY;
ALTER TABLE searches            ENABLE ROW LEVEL SECURITY;
ALTER TABLE search_candidates   ENABLE ROW LEVEL SECURITY;
ALTER TABLE assessments         ENABLE ROW LEVEL SECURITY;
ALTER TABLE assessment_evidence ENABLE ROW LEVEL SECURITY;
