-- 009: Deep research as an explicit, tracked background job per company.
--
-- Level 3 research (full registers, filed accounts read by AI, more web
-- pages and news) runs only when the analyst asks for it. These columns
-- record its progress and what it actually cost, so the company page can
-- show "running", "done on <date> for $0.14" or why it failed.

ALTER TABLE companies
    ADD COLUMN deep_status      TEXT CHECK (deep_status IN ('running', 'complete', 'failed')),
    ADD COLUMN deep_message     TEXT,         -- progress, summary or error
    ADD COLUMN deep_started_at  TIMESTAMPTZ,
    ADD COLUMN deep_finished_at TIMESTAMPTZ,
    ADD COLUMN deep_cost_usd    NUMERIC(10, 6);
