-- 008: Buyer brief and a readable target report.
--
--   * searches     gain an optional buyer brief: the analyst's description of
--                  the buyer's strategy and what it is looking for. It is
--                  given to Claude as evidence (labelled as the analyst's
--                  statement, not verified) and can be edited later.
--   * assessments  (the overall row) gain a one-line headline and
--                  highlights: strengths, risks and what to check next. Any
--                  row can record that it relied on the buyer brief.


ALTER TABLE searches
    ADD COLUMN buyer_brief            TEXT,
    ADD COLUMN buyer_brief_updated_at TIMESTAMPTZ;

ALTER TABLE assessments
    ADD COLUMN headline    TEXT,       -- one-line verdict (overall row)
    ADD COLUMN highlights  JSONB,      -- {"strengths": [{"text", "evidence_ids"}], "risks": [...], "next_checks": [text]}
    ADD COLUMN cites_brief BOOLEAN NOT NULL DEFAULT FALSE;
