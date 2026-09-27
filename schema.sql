CREATE TABLE IF NOT EXISTS companies (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    website TEXT,
    sector TEXT,
    country TEXT,
    revenue TEXT,
    employees TEXT,
    description TEXT
);
