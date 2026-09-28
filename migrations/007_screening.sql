-- 007: Level 2 screening and AI assessments.
--
--   * events       gain the quote that supports them and which provider
--                  wrote them, so news re-runs replace their own events only.
--   * assessments  separate what the cited evidence says (facts_summary)
--                  from the judgement drawn from it (interpretation), and
--                  record which prompt version produced an AI assessment.
--   * search_candidates  record when a candidate was screened, or why
--                  screening failed.


ALTER TABLE events
    ADD COLUMN excerpt      TEXT,      -- supporting quote from the source
    ADD COLUMN extracted_by TEXT;      -- provider that wrote it, e.g. news; NULL for analyst entries

CREATE INDEX events_company_extractor_idx ON events (company_id, extracted_by);


ALTER TABLE assessments
    ADD COLUMN facts_summary  TEXT,    -- what the cited evidence shows (no judgement)
    ADD COLUMN interpretation TEXT,    -- the judgement drawn from it, relative to the buyer
    ADD COLUMN prompt_version TEXT;    -- AI assessments: which prompt produced it


ALTER TABLE search_candidates
    ADD COLUMN screened_at     TIMESTAMPTZ,
    ADD COLUMN screening_error TEXT;
