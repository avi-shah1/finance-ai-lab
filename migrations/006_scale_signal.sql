-- 006: A fact type for size signals found during discovery, e.g. "team of
-- 120", "used by 5,000 GP practices" or "Series B". These are quoted from
-- the company's own page; exact figures come later from filings.

INSERT INTO fact_types (code, label, category, value_kind, multi_valued, sort_order) VALUES
    ('scale_signal', 'Scale signal', 'market', 'text', TRUE, 64);
