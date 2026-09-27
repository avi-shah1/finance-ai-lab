-- 004: AI-assisted extraction from documents and web pages already retrieved.
--
--   * facts record HOW each value was obtained (extraction_method), which
--     provider wrote it (extracted_by), the AI model when one was used,
--     whether the supporting quote was found in the source text
--     (verification) and the page it came from (evidence_page).
--   * sources record the exact content that was retrieved (a cached copy
--     and its SHA-256), and which version the AI last extracted from, so an
--     unchanged document is never sent to the model twice.


ALTER TABLE facts
    ADD COLUMN extraction_method TEXT CHECK (extraction_method IN (
                   'manual',            -- typed in by an analyst
                   'structured_api',    -- a field from a register/API response (e.g. CH profile)
                   'xbrl_tag',          -- a value the filer tagged in XBRL/iXBRL
                   'html_rule',         -- deterministic rule over a web page
                   'third_party_api',   -- a data vendor's API (yfinance)
                   'llm_document',      -- AI-extracted from a filed document
                   'llm_website'        -- AI-extracted from a company web page
               )),
    ADD COLUMN extracted_by      TEXT,  -- provider that wrote the fact; re-runs only replace their own
    ADD COLUMN extraction_model  TEXT,  -- e.g. claude-opus-5 (AI facts only)
    ADD COLUMN verification      TEXT CHECK (verification IN (
                   'quote_verified',    -- supporting quote found in the source text
                   'not_verifiable'     -- source has no text layer (scanned); quote unchecked
               )),
    ADD COLUMN evidence_page     INT;   -- 1-based page in the original document

-- Backfill existing facts from their source's provider.
UPDATE facts f
SET extracted_by = s.provider,
    extraction_method = CASE
        WHEN s.provider = 'manual' THEN 'manual'
        WHEN s.provider = 'yfinance' THEN 'third_party_api'
        WHEN s.provider = 'website' THEN 'html_rule'
        WHEN f.location LIKE 'iXBRL tag%' OR f.location LIKE 'XBRL %' THEN 'xbrl_tag'
        ELSE 'structured_api'
    END
FROM sources s
WHERE s.id = f.source_id;

CREATE INDEX facts_source_extractor_idx ON facts (source_id, extracted_by);


ALTER TABLE sources
    ADD COLUMN content_path        TEXT,   -- local cached copy of what was retrieved
    ADD COLUMN content_sha256      TEXT,   -- fingerprint of that content
    ADD COLUMN ai_extracted_sha256 TEXT;   -- content version the AI last extracted from


INSERT INTO fact_types (code, label, category, value_kind, multi_valued, sort_order) VALUES
    ('creditors_due_within_one_year', 'Creditors due within one year', 'financial', 'money', FALSE, 40),
    ('creditors_due_after_one_year',  'Creditors due after one year',  'financial', 'money', FALSE, 41);
