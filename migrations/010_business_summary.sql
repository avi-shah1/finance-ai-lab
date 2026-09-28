-- 010: "What they do" as 2-3 neutral bullets written by Claude in the third
-- person, rather than the company's own marketing copy ("helps you...").
-- value_text holds the bullets separated by newlines. Written at discovery
-- (from the search result) and again at screening / deep research (from
-- the company's own pages); the newest is shown.

INSERT INTO fact_types (code, label, category, value_kind, multi_valued, sort_order) VALUES
    ('business_summary', 'What they do', 'identity', 'text', FALSE, 9);
