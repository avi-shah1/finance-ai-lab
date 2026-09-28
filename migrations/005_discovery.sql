-- 005: Automatic target discovery and cost tracking.
--
--   * companies    gain a normalised web domain (how discovered companies are
--                  de-duplicated across searches and sources) and a research
--                  level. Discovered companies are stored automatically as
--                  lightweight rows; the company library shows only the ones
--                  that were shortlisted or deep-researched.
--   * searches     gain optional acquisition criteria and background-job
--                  progress (status, message, error, timings), plus a log of
--                  pipeline decisions such as "Brave skipped: Exa found 18".
--   * search_candidates  gain a screening stage, why the company matched,
--                  a relevance label, which search sources found it and a rank.
--   * discovery_hits  every raw search-API result, so each candidate can show
--                  where it came from and the thresholds can be tuned later.
--   * api_usage    one row per paid API call (Claude, Exa, Brave) with tokens
--                  or units and an estimated cost in US dollars.


-- ---------------------------------------------------------------------------
-- Companies: domain + research level
-- ---------------------------------------------------------------------------

ALTER TABLE companies
    ADD COLUMN domain         TEXT,     -- e.g. accurx.com: lower-case, no scheme, no www., no path
    ADD COLUMN research_level TEXT NOT NULL DEFAULT 'discovered' CHECK (research_level IN (
                                  'discovered',   -- found by a search; name/website/snippet only
                                  'screened',     -- lightweight evidence gathered and assessed
                                  'deep'          -- full research (filings, documents) has run
                              ));

-- Existing companies were all added to the library deliberately.
UPDATE companies SET research_level = 'deep';

-- Backfill the domain from the stored website.
UPDATE companies
SET domain = nullif(split_part(regexp_replace(regexp_replace(lower(trim(website)),
                  '^https?://', ''), '^www\.', ''), '/', 1), '')
WHERE website IS NOT NULL;

CREATE UNIQUE INDEX companies_domain_key ON companies (domain) WHERE domain IS NOT NULL;


-- ---------------------------------------------------------------------------
-- Searches: criteria + background job progress
-- ---------------------------------------------------------------------------

ALTER TABLE searches
    ADD COLUMN criteria       TEXT,     -- optional free-text acquisition criteria
    ADD COLUMN status         TEXT NOT NULL DEFAULT 'queued' CHECK (status IN (
                                  'queued', 'discovering', 'screening', 'complete', 'failed'
                              )),
    ADD COLUMN status_message TEXT,     -- e.g. "Screening 7 of 20 candidates"
    ADD COLUMN error          TEXT,
    ADD COLUMN decisions      JSONB NOT NULL DEFAULT '[]',  -- [{"step": "brave", "decision": "skipped", "reason": "..."}]
    ADD COLUMN started_at     TIMESTAMPTZ,
    ADD COLUMN finished_at    TIMESTAMPTZ;

-- Searches created before discovery existed have nothing left to run.
UPDATE searches SET status = 'complete';


-- ---------------------------------------------------------------------------
-- Search candidates: stage, match reason, relevance, sources, rank
-- ---------------------------------------------------------------------------

ALTER TABLE search_candidates
    ADD COLUMN stage          TEXT NOT NULL DEFAULT 'discovered' CHECK (stage IN (
                                  'discovered',     -- passed triage, waiting for screening
                                  'filtered_out',   -- triage judged it out of scope (kept for tuning)
                                  'screened',       -- evidence gathered and assessed
                                  'shortlisted',    -- analyst marked it for follow-up
                                  'dismissed'       -- analyst ruled it out
                              )),
    ADD COLUMN match_reason   TEXT,     -- why it matched the search, in a sentence
    ADD COLUMN relevance      TEXT CHECK (relevance IN ('high', 'medium', 'low')),
    ADD COLUMN discovered_via TEXT[] NOT NULL DEFAULT '{}',  -- e.g. {exa,brave}; manual adds stay empty
    ADD COLUMN rank           INT;      -- display order within the search

UPDATE search_candidates SET stage = 'screened' WHERE origin = 'manual';


-- ---------------------------------------------------------------------------
-- Discovery hits: raw search results
-- ---------------------------------------------------------------------------

CREATE TABLE discovery_hits (
    id           BIGSERIAL PRIMARY KEY,
    search_id    INT  NOT NULL REFERENCES searches (id) ON DELETE CASCADE,
    source       TEXT NOT NULL CHECK (source IN ('exa', 'brave')),
    purpose      TEXT NOT NULL DEFAULT 'discovery' CHECK (purpose IN ('discovery', 'news', 'buyer')),
    query        TEXT NOT NULL,
    result_rank  INT,                   -- position in that query's results (1 = first)
    url          TEXT NOT NULL,
    title        TEXT,
    snippet      TEXT,
    domain       TEXT,
    company_id   INT REFERENCES companies (id) ON DELETE SET NULL,  -- set once identified
    retrieved_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX discovery_hits_search_idx  ON discovery_hits (search_id);
CREATE INDEX discovery_hits_company_idx ON discovery_hits (company_id);


-- ---------------------------------------------------------------------------
-- API usage and cost
-- ---------------------------------------------------------------------------

CREATE TABLE api_usage (
    id                 BIGSERIAL PRIMARY KEY,
    service            TEXT NOT NULL CHECK (service IN ('anthropic', 'exa', 'brave')),
    task               TEXT NOT NULL,       -- e.g. triage, assessment, deep_documents, discovery_search
    model              TEXT,                -- Claude model that answered (anthropic only)
    input_tokens       INT NOT NULL DEFAULT 0,
    output_tokens      INT NOT NULL DEFAULT 0,
    cache_read_tokens  INT NOT NULL DEFAULT 0,
    cache_write_tokens INT NOT NULL DEFAULT 0,
    units              INT NOT NULL DEFAULT 0,  -- searches or pages (exa/brave)
    cost_usd           NUMERIC(10, 6),          -- estimate at list prices; NULL if the price is unknown
    search_id          INT REFERENCES searches (id) ON DELETE SET NULL,
    company_id         INT REFERENCES companies (id) ON DELETE SET NULL,
    request_id         TEXT,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX api_usage_search_idx  ON api_usage (search_id);
CREATE INDEX api_usage_company_idx ON api_usage (company_id);


-- ---------------------------------------------------------------------------
-- Facts: new extraction methods for the cheaper screening pipeline
-- ---------------------------------------------------------------------------

ALTER TABLE facts DROP CONSTRAINT facts_extraction_method_check;
ALTER TABLE facts ADD CONSTRAINT facts_extraction_method_check CHECK (extraction_method IN (
    'manual', 'structured_api', 'xbrl_tag', 'html_rule', 'third_party_api',
    'llm_document',        -- AI-extracted from a whole filed document (deep research)
    'llm_document_text',   -- AI-extracted from the text of selected pages (cheap screening mode)
    'llm_website',         -- AI-extracted from a company web page
    'llm_search_result'    -- AI-extracted from a search result's text (e.g. a news article)
));


-- ---------------------------------------------------------------------------
-- Supabase: keep the new tables off the public Data API
-- ---------------------------------------------------------------------------

ALTER TABLE discovery_hits ENABLE ROW LEVEL SECURITY;
ALTER TABLE api_usage      ENABLE ROW LEVEL SECURITY;
