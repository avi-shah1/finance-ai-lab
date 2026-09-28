"""All SQL lives here. Routes in app.py call these functions and never write SQL."""

import os
import threading
import time
from collections import defaultdict
from contextlib import contextmanager

import psycopg2
import psycopg2.extras
import psycopg2.pool
from dotenv import load_dotenv
from psycopg2.extras import Json, execute_values

from domains import normalise_domain

load_dotenv()

DATABASE_URL = os.environ["DATABASE_URL"]

# Opening a connection to the hosted database takes ~0.5 s, so connections
# are kept open in a pool and reused. TCP keepalives stop idle connections
# being silently dropped; one idle for longer than MAX_IDLE_SECONDS is
# replaced anyway rather than trusted.
POOL_MAX = 10
MAX_IDLE_SECONDS = 240
_KEEPALIVES = {"keepalives": 1, "keepalives_idle": 30, "keepalives_interval": 10, "keepalives_count": 3}
_pool = None
_pool_lock = threading.Lock()
_last_used = {}   # id(connection) -> time it went back into the pool


def get_connection():
    """A fresh, unpooled connection (for scripts that manage their own)."""
    return psycopg2.connect(DATABASE_URL, **_KEEPALIVES)


def _get_pool():
    global _pool
    with _pool_lock:
        if _pool is None:
            _pool = psycopg2.pool.ThreadedConnectionPool(1, POOL_MAX, DATABASE_URL, **_KEEPALIVES)
        return _pool


@contextmanager
def cursor(readonly=False):
    """Yield a cursor that returns rows as dicts, on a pooled connection.

    Normally everything inside the `with` block is one transaction: it
    commits on success and rolls back if anything raises. That costs two
    extra round trips (BEGIN and COMMIT), so single-statement reads pass
    readonly=True and run without one. A broken connection is closed
    rather than reused."""
    pool = _get_pool()
    conn = pool.getconn()
    idle_since = _last_used.pop(id(conn), None)
    if conn.closed or (idle_since is not None and time.monotonic() - idle_since > MAX_IDLE_SECONDS):
        pool.putconn(conn, close=True)
        conn = pool.getconn()
    broken = False
    try:
        if readonly:
            conn.autocommit = True
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                yield cur
        else:
            with conn:
                with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    yield cur
    except (psycopg2.OperationalError, psycopg2.InterfaceError):
        broken = True
        raise
    finally:
        if broken or conn.closed:
            pool.putconn(conn, close=True)
        else:
            conn.autocommit = False
            _last_used[id(conn)] = time.monotonic()
            pool.putconn(conn)


# ---------------------------------------------------------------------------
# Companies
# ---------------------------------------------------------------------------

def get_all_companies(library_only=False):
    """Every company, or with library_only just the company library: those
    added by hand or researched beyond discovery. Companies a search merely
    discovered stay out of the library until they are screened."""
    with cursor(readonly=True) as cur:
        cur.execute(
            "SELECT * FROM companies"
            + (" WHERE research_level <> 'discovered'" if library_only else "")
            + " ORDER BY name;"
        )
        return cur.fetchall()


def get_company(company_id):
    with cursor(readonly=True) as cur:
        cur.execute("SELECT * FROM companies WHERE id = %s;", (company_id,))
        return cur.fetchone()


def find_or_create_company(name):
    """Reuse an existing company with the same name (case-insensitive)."""
    with cursor() as cur:
        cur.execute(
            "SELECT id FROM companies WHERE lower(name) = lower(%s) ORDER BY id LIMIT 1;",
            (name,),
        )
        row = cur.fetchone()
    return row["id"] if row else create_company(name)


# ---------------------------------------------------------------------------
# Evidence: sources, facts, events
# ---------------------------------------------------------------------------

def get_fact_types():
    with cursor(readonly=True) as cur:
        cur.execute("SELECT * FROM fact_types ORDER BY sort_order;")
        return cur.fetchall()


# A fact or event is always saved in the same transaction as its source, so
# the database can never hold a source-less claim or an orphaned source.
#
#   source = {"source_type", "name", "url", "title", "published_on"}
#   fact   = {"fact_type", "value_text", "evidence_type", and optionally
#             "value_number", "currency", "period", "as_of", "excerpt"}
#   event  = {"event_type", "title", "evidence_type", and optionally
#             "summary", "event_date"}

def _insert_source(cur, company_id, source):
    cur.execute(
        """
        INSERT INTO sources (company_id, provider, source_type, name, url, title, published_on)
        VALUES (%(company_id)s, 'manual', %(source_type)s, %(name)s, %(url)s, %(title)s, %(published_on)s)
        RETURNING id;
        """,
        {**source, "company_id": company_id},
    )
    return cur.fetchone()["id"]


def _insert_fact(cur, company_id, source_id, fact):
    cur.execute(
        """
        INSERT INTO facts (company_id, fact_type, value_text, value_number, currency,
                           period, as_of, source_id, evidence_type, excerpt,
                           extracted_by, extraction_method)
        VALUES (%(company_id)s, %(fact_type)s, %(value_text)s, %(value_number)s, %(currency)s,
                %(period)s, %(as_of)s, %(source_id)s, %(evidence_type)s, %(excerpt)s,
                'manual', 'manual');
        """,
        {"value_number": None, "currency": None, "period": None, "as_of": None, "excerpt": None,
         **fact, "company_id": company_id, "source_id": source_id},
    )


def record_facts(company_id, source, facts):
    """Save one source and the facts it supports."""
    with cursor() as cur:
        source_id = _insert_source(cur, company_id, source)
        for fact in facts:
            _insert_fact(cur, company_id, source_id, fact)


def record_event(company_id, source, event):
    with cursor() as cur:
        source_id = _insert_source(cur, company_id, source)
        cur.execute(
            """
            INSERT INTO events (company_id, event_type, title, summary, event_date,
                                source_id, evidence_type)
            VALUES (%(company_id)s, %(event_type)s, %(title)s, %(summary)s, %(event_date)s,
                    %(source_id)s, %(evidence_type)s);
            """,
            {"summary": None, "event_date": None,
             **event, "company_id": company_id, "source_id": source_id},
        )


def create_company(name, website=None, hq_country=None, sector=None,
                   source=None, facts=(), search_id=None):
    """Create a company added by hand (it joins the company library),
    optionally with sourced facts, and optionally add it to a search's
    candidate list, all in one transaction. If a search already discovered
    a company with the same website domain, that row is reused and moved
    into the library instead of creating a duplicate."""
    domain = normalise_domain(website)
    with cursor() as cur:
        cur.execute(
            """
            INSERT INTO companies (name, website, hq_country, sector, domain, research_level)
            VALUES (%s, %s, %s, %s, %s, 'screened')
            ON CONFLICT (domain) WHERE domain IS NOT NULL DO UPDATE
                SET research_level = CASE WHEN companies.research_level = 'discovered'
                                          THEN 'screened' ELSE companies.research_level END,
                    hq_country = coalesce(companies.hq_country, EXCLUDED.hq_country),
                    sector = coalesce(companies.sector, EXCLUDED.sector)
            RETURNING id;
            """,
            (name, website, hq_country, sector, domain),
        )
        company_id = cur.fetchone()["id"]
        if facts:
            source_id = _insert_source(cur, company_id, source)
            for fact in facts:
                _insert_fact(cur, company_id, source_id, fact)
        if search_id is not None:
            cur.execute(
                "INSERT INTO search_candidates (search_id, company_id, stage) VALUES (%s, %s, 'screened') "
                "ON CONFLICT DO NOTHING;",
                (search_id, company_id),
            )
        return company_id


# Columns every fact/event query returns about its source, so templates can
# render the same source component everywhere.
_SOURCE_COLUMNS = """
    s.id AS source_id, s.source_type, s.name AS source_name, s.title AS source_title,
    s.url AS source_url, s.published_on AS source_published_on,
    s.retrieved_at AS source_retrieved_at, s.provider, s.document_type AS source_document_type
"""


def get_facts_for(company_ids):
    """Return {company_id: [fact, ...]} with source details joined on.
    Within each company, the most recent value of a fact type comes first."""
    if not company_ids:
        return {}
    with cursor(readonly=True) as cur:
        cur.execute(
            f"""
            SELECT f.*, ft.label, ft.category, ft.value_kind, ft.multi_valued, {_SOURCE_COLUMNS}
            FROM facts f
            JOIN fact_types ft ON ft.code = f.fact_type
            JOIN sources s ON s.id = f.source_id
            WHERE f.company_id = ANY(%s)
            ORDER BY ft.sort_order, coalesce(f.period_end, f.as_of) DESC NULLS LAST, f.created_at DESC;
            """,
            (list(company_ids),),
        )
        rows = cur.fetchall()
    grouped = defaultdict(list)
    for row in rows:
        grouped[row["company_id"]].append(row)
    return grouped


def get_facts_by_id(fact_ids):
    """{fact_id: fact row with source details} for cited evidence."""
    if not fact_ids:
        return {}
    with cursor(readonly=True) as cur:
        cur.execute(
            f"""
            SELECT f.*, ft.label, ft.category, ft.value_kind, ft.multi_valued, c.name AS company_name,
                   {_SOURCE_COLUMNS}
            FROM facts f
            JOIN fact_types ft ON ft.code = f.fact_type
            JOIN sources s ON s.id = f.source_id
            JOIN companies c ON c.id = f.company_id
            WHERE f.id = ANY(%s);
            """,
            (list(fact_ids),),
        )
        return {r["id"]: r for r in cur.fetchall()}


def get_events_by_id(event_ids):
    """{event_id: event row with source details} for cited evidence."""
    if not event_ids:
        return {}
    with cursor(readonly=True) as cur:
        cur.execute(
            f"""
            SELECT e.*, c.name AS company_name, {_SOURCE_COLUMNS}
            FROM events e
            JOIN sources s ON s.id = e.source_id
            JOIN companies c ON c.id = e.company_id
            WHERE e.id = ANY(%s);
            """,
            (list(event_ids),),
        )
        return {r["id"]: r for r in cur.fetchall()}


def get_events_for(company_ids):
    """Return {company_id: [event, ...]} newest first, with source details."""
    if not company_ids:
        return {}
    with cursor(readonly=True) as cur:
        cur.execute(
            f"""
            SELECT e.*, {_SOURCE_COLUMNS}
            FROM events e
            JOIN sources s ON s.id = e.source_id
            WHERE e.company_id = ANY(%s)
            ORDER BY e.event_date DESC NULLS LAST, e.created_at DESC;
            """,
            (list(company_ids),),
        )
        rows = cur.fetchall()
    grouped = defaultdict(list)
    for row in rows:
        grouped[row["company_id"]].append(row)
    return grouped


def get_sources(company_id):
    """Every source recorded for a company, newest first, with the fact
    types each one supports (for the Sources / Evidence register)."""
    with cursor(readonly=True) as cur:
        cur.execute(
            """
            SELECT s.*,
                   coalesce(array_agg(DISTINCT ft.label) FILTER (WHERE ft.label IS NOT NULL), '{}') AS supports,
                   coalesce(array_agg(DISTINCT f.fact_type) FILTER (WHERE f.extracted_by IN ('ai_documents', 'ai_website')), '{}') AS ai_fact_types,
                   count(f.id) AS fact_count,
                   count(f.id) FILTER (WHERE f.extracted_by IN ('ai_documents', 'ai_website')) AS ai_fact_count
            FROM sources s
            LEFT JOIN facts f ON f.source_id = s.id
            LEFT JOIN fact_types ft ON ft.code = f.fact_type
            WHERE s.company_id = %s
            GROUP BY s.id
            ORDER BY s.document_type IS NULL, s.period_end DESC NULLS LAST,
                     s.published_on DESC NULLS LAST, s.retrieved_at DESC;
            """,
            (company_id,),
        )
        return cur.fetchall()


# ---------------------------------------------------------------------------
# Identifiers and research runs
# ---------------------------------------------------------------------------

def get_identifiers(company_id, detailed=False):
    """{scheme: value}, or {scheme: row} when detailed=True."""
    with cursor(readonly=True) as cur:
        cur.execute("SELECT * FROM company_identifiers WHERE company_id = %s;", (company_id,))
        rows = cur.fetchall()
    return {r["scheme"]: (r if detailed else r["value"]) for r in rows}


def set_identifier(company_id, scheme, value):
    """Record an analyst-entered identifier; an empty value removes it."""
    with cursor() as cur:
        if value:
            cur.execute(
                """
                INSERT INTO company_identifiers (company_id, scheme, value, match_method)
                VALUES (%s, %s, %s, 'analyst_entered')
                ON CONFLICT (company_id, scheme) DO UPDATE
                    SET value = EXCLUDED.value, match_method = 'analyst_entered';
                """,
                (company_id, scheme, value),
            )
        else:
            cur.execute("DELETE FROM company_identifiers WHERE company_id = %s AND scheme = %s;",
                        (company_id, scheme))


def set_website_if_missing(company_id, website):
    """Record a company's website (and domain) unless one is already set."""
    domain = normalise_domain(website)
    with cursor() as cur:
        cur.execute(
            """
            UPDATE companies
            SET website = %(website)s,
                domain = CASE WHEN EXISTS (SELECT 1 FROM companies o WHERE o.domain = %(domain)s AND o.id <> %(id)s)
                              THEN domain ELSE %(domain)s END
            WHERE id = %(id)s AND coalesce(website, '') = '';
            """,
            {"website": website, "domain": domain, "id": company_id},
        )


def set_derived_identifier(company_id, scheme, value):
    """Record an identifier read from another source (e.g. a company number
    printed on the company's website). Never replaces an existing one."""
    with cursor() as cur:
        cur.execute(
            """
            INSERT INTO company_identifiers (company_id, scheme, value, match_method)
            VALUES (%s, %s, %s, 'derived')
            ON CONFLICT (company_id, scheme) DO NOTHING;
            """,
            (company_id, scheme, value),
        )


def update_company_identity(company_id, website, hq_country, sector):
    """The domain follows the website, unless another company already has
    that domain (then it is left as it was rather than breaking uniqueness)."""
    with cursor() as cur:
        cur.execute(
            """
            UPDATE companies SET website = %(website)s, hq_country = %(hq)s, sector = %(sector)s,
                domain = CASE
                    WHEN %(domain)s IS NULL THEN NULL
                    WHEN EXISTS (SELECT 1 FROM companies o WHERE o.domain = %(domain)s AND o.id <> %(id)s)
                        THEN domain
                    ELSE %(domain)s END
            WHERE id = %(id)s;
            """,
            {"website": website, "hq": hq_country, "sector": sector,
             "domain": normalise_domain(website), "id": company_id},
        )


def get_latest_runs(company_ids):
    """{company_id: {provider: latest research_runs row}}."""
    if not company_ids:
        return {}
    with cursor(readonly=True) as cur:
        cur.execute(
            """
            SELECT DISTINCT ON (company_id, provider) *
            FROM research_runs
            WHERE company_id = ANY(%s) AND status <> 'running'
            ORDER BY company_id, provider, started_at DESC;
            """,
            (list(company_ids),),
        )
        rows = cur.fetchall()
    grouped = defaultdict(dict)
    for row in rows:
        grouped[row["company_id"]][row["provider"]] = row
    return grouped


# ---------------------------------------------------------------------------
# Searches and candidates
# ---------------------------------------------------------------------------

def insert_search(buyer_company_id, target_sector, geography, criteria=None, buyer_brief=None):
    with cursor() as cur:
        cur.execute(
            """
            INSERT INTO searches (buyer_company_id, target_sector, geography, criteria,
                                  buyer_brief, buyer_brief_updated_at)
            VALUES (%s, %s, %s, %s, %s, CASE WHEN %s IS NULL THEN NULL ELSE now() END)
            RETURNING id;
            """,
            (buyer_company_id, target_sector, geography, criteria, buyer_brief, buyer_brief),
        )
        return cur.fetchone()["id"]


def update_buyer_brief(search_id, brief):
    with cursor() as cur:
        cur.execute("UPDATE searches SET buyer_brief = %s, buyer_brief_updated_at = now() WHERE id = %s;",
                    (brief, search_id))


def get_ai_headlines(search_id):
    """{company_id: latest AI overall row (headline, highlights)} for a search,
    shown in the results table even when the analyst has overridden the rating."""
    with cursor(readonly=True) as cur:
        cur.execute(
            """
            SELECT DISTINCT ON (company_id) company_id, headline, rationale, created_at
            FROM assessments
            WHERE search_id = %s AND dimension = 'overall' AND assessed_by = 'ai'
            ORDER BY company_id, created_at DESC;
            """,
            (search_id,),
        )
        return {r["company_id"]: r for r in cur.fetchall()}


def set_search_status(search_id, status, message=None, error=None):
    """Move a search's background job along. 'discovering' stamps the start
    time; 'complete' and 'failed' stamp the finish time."""
    with cursor() as cur:
        cur.execute(
            """
            UPDATE searches
            SET status = %(status)s, status_message = %(message)s, error = %(error)s,
                started_at = CASE WHEN %(status)s = 'discovering' AND status <> 'discovering'
                                  THEN now() ELSE started_at END,
                finished_at = CASE WHEN %(status)s IN ('complete', 'failed') THEN now() ELSE NULL END
            WHERE id = %(id)s;
            """,
            {"status": status, "message": message, "error": error, "id": search_id},
        )


def add_search_decision(search_id, step, decision, reason):
    """Append to the search's decision log, e.g. ("brave", "skipped", "Exa found 18")."""
    with cursor() as cur:
        cur.execute(
            "UPDATE searches SET decisions = decisions || %s WHERE id = %s;",
            (Json([{"step": step, "decision": decision, "reason": reason}]), search_id),
        )


def fail_unfinished_searches(message):
    """Background jobs die with the app process. Called at startup so a
    search interrupted by a restart says so instead of spinning forever."""
    with cursor() as cur:
        cur.execute(
            """
            UPDATE searches SET status = 'failed', error = %s, finished_at = now()
            WHERE status IN ('queued', 'discovering', 'screening');
            """,
            (message,),
        )
        return cur.rowcount


def reset_discovery(search_id):
    """Clear a search's previous discovery results before running it again.
    Manually added candidates are kept; discovered companies stay in the
    database (they are a cache), only their link to this search goes."""
    with cursor() as cur:
        cur.execute("DELETE FROM discovery_hits WHERE search_id = %s;", (search_id,))
        cur.execute("DELETE FROM search_candidates WHERE search_id = %s AND origin = 'discovery';",
                    (search_id,))
        cur.execute("UPDATE searches SET decisions = '[]' WHERE id = %s;", (search_id,))


def upsert_discovered_companies(rows):
    """rows: [(name, domain, website)] with distinct domains. Reuses the
    company already holding each domain, or creates a lightweight
    'discovered' row. One round trip. Returns {domain: company_id}."""
    if not rows:
        return {}
    with cursor() as cur:
        result = execute_values(
            cur,
            """
            INSERT INTO companies (name, domain, website, research_level)
            VALUES %s
            ON CONFLICT (domain) WHERE domain IS NOT NULL
            DO UPDATE SET website = coalesce(companies.website, EXCLUDED.website)
            RETURNING id, domain;
            """,
            [(name, domain, website) for name, domain, website in rows],
            template="(%s, %s, %s, 'discovered')",
            fetch=True,
        )
        return {r["domain"]: r["id"] for r in result}


def fill_hq_countries(countries):
    """{company_id: country}: set the HQ country where none is recorded yet
    (a value entered by the analyst is never replaced)."""
    if not countries:
        return
    with cursor() as cur:
        execute_values(
            cur,
            """
            UPDATE companies c SET hq_country = v.country
            FROM (VALUES %s) AS v(id, country)
            WHERE c.id = v.id AND coalesce(c.hq_country, '') = '';
            """,
            list(countries.items()),
        )


def insert_discovery_hits(search_id, rows):
    """rows: dicts with source, purpose, query, result_rank, url, title,
    snippet, domain, company_id."""
    if not rows:
        return
    with cursor() as cur:
        execute_values(
            cur,
            """
            INSERT INTO discovery_hits (search_id, source, purpose, query, result_rank, url,
                                        title, snippet, domain, company_id)
            VALUES %s;
            """,
            [(search_id, r["source"], r["purpose"], r["query"], r["result_rank"], r["url"],
              r["title"], r["snippet"], r["domain"], r["company_id"]) for r in rows],
            page_size=500,
        )


def upsert_search_candidates(search_id, rows):
    """rows: dicts with company_id, stage, match_reason, relevance, sources,
    rank. One round trip. A manually added candidate is left as it is."""
    if not rows:
        return
    with cursor() as cur:
        execute_values(
            cur,
            """
            INSERT INTO search_candidates (search_id, company_id, origin, stage, match_reason,
                                           relevance, discovered_via, rank)
            VALUES %s
            ON CONFLICT (search_id, company_id) DO UPDATE
                SET stage = EXCLUDED.stage, match_reason = EXCLUDED.match_reason,
                    relevance = EXCLUDED.relevance, rank = EXCLUDED.rank,
                    discovered_via = ARRAY(SELECT DISTINCT unnest(search_candidates.discovered_via
                                                                  || EXCLUDED.discovered_via))
                WHERE search_candidates.origin = 'discovery';
            """,
            [(search_id, r["company_id"], r["stage"], r["match_reason"], r["relevance"],
              list(r["sources"]), r["rank"]) for r in rows],
            template="(%s, %s, 'discovery', %s, %s, %s, %s::text[], %s)",
        )


_SEARCH_SELECT = """
    SELECT s.*, b.name AS buyer_name,
           (SELECT count(*) FROM search_candidates sc WHERE sc.search_id = s.id) AS candidate_count
    FROM searches s
    JOIN companies b ON b.id = s.buyer_company_id
"""


def get_search(search_id):
    with cursor(readonly=True) as cur:
        cur.execute(_SEARCH_SELECT + " WHERE s.id = %s;", (search_id,))
        return cur.fetchone()


def get_recent_searches(limit=10):
    with cursor(readonly=True) as cur:
        cur.execute(_SEARCH_SELECT + " ORDER BY s.created_at DESC LIMIT %s;", (limit,))
        return cur.fetchall()


def get_searches_for_buyer(buyer_company_id):
    with cursor(readonly=True) as cur:
        cur.execute(_SEARCH_SELECT + " WHERE s.buyer_company_id = %s ORDER BY s.created_at DESC;",
                    (buyer_company_id,))
        return cur.fetchall()


def get_candidates(search_id):
    """Candidates in display order, each with its search-specific fields
    and the URLs of the search results that found it."""
    with cursor(readonly=True) as cur:
        cur.execute(
            """
            SELECT c.*, sc.origin, sc.added_at, sc.stage, sc.match_reason, sc.relevance,
                   sc.discovered_via, sc.rank, sc.screened_at, sc.screening_error,
                   coalesce((SELECT array_agg(DISTINCT h.url) FROM discovery_hits h
                             WHERE h.search_id = sc.search_id AND h.company_id = sc.company_id),
                            '{}') AS hit_urls
            FROM search_candidates sc
            JOIN companies c ON c.id = sc.company_id
            WHERE sc.search_id = %s
            ORDER BY sc.rank NULLS LAST, c.name;
            """,
            (search_id,),
        )
        return cur.fetchall()


def add_candidate(search_id, company_id, origin="manual"):
    with cursor() as cur:
        cur.execute(
            """
            INSERT INTO search_candidates (search_id, company_id, origin)
            VALUES (%s, %s, %s)
            ON CONFLICT DO NOTHING;
            """,
            (search_id, company_id, origin),
        )


def remove_candidate(search_id, company_id):
    with cursor() as cur:
        cur.execute(
            "DELETE FROM search_candidates WHERE search_id = %s AND company_id = %s;",
            (search_id, company_id),
        )


# ---------------------------------------------------------------------------
# API usage and cost (written through usage.py)
# ---------------------------------------------------------------------------

def insert_api_usage(row):
    with cursor() as cur:
        cur.execute(
            """
            INSERT INTO api_usage (service, task, model, input_tokens, output_tokens,
                                   cache_read_tokens, cache_write_tokens, units, cost_usd,
                                   search_id, company_id, request_id)
            VALUES (%(service)s, %(task)s, %(model)s, %(input_tokens)s, %(output_tokens)s,
                    %(cache_read_tokens)s, %(cache_write_tokens)s, %(units)s, %(cost_usd)s,
                    %(search_id)s, %(company_id)s, %(request_id)s);
            """,
            row,
        )


def get_usage_summary(search_id=None, company_id=None):
    """Estimated spend per service and task, for one search or one company."""
    column, value = ("search_id", search_id) if search_id is not None else ("company_id", company_id)
    with cursor(readonly=True) as cur:
        cur.execute(
            f"""
            SELECT service, task, count(*) AS calls, sum(units) AS units,
                   sum(input_tokens) AS input_tokens, sum(output_tokens) AS output_tokens,
                   sum(cost_usd) AS cost_usd
            FROM api_usage
            WHERE {column} = %s
            GROUP BY service, task
            ORDER BY sum(cost_usd) DESC NULLS LAST;
            """,
            (value,),
        )
        return cur.fetchall()


def mark_screened(search_id, company_id, error=None):
    """Record the outcome of screening a candidate. On success the candidate
    moves to 'screened' and the company out of the hidden 'discovered' level."""
    with cursor() as cur:
        cur.execute(
            """
            UPDATE search_candidates
            SET screened_at = CASE WHEN %(error)s IS NULL THEN now() ELSE screened_at END,
                screening_error = %(error)s,
                stage = CASE WHEN %(error)s IS NULL AND stage = 'discovered' THEN 'screened' ELSE stage END
            WHERE search_id = %(search_id)s AND company_id = %(company_id)s;
            """,
            {"error": error, "search_id": search_id, "company_id": company_id},
        )
        if error is None:
            cur.execute("UPDATE companies SET research_level = 'screened' "
                        "WHERE id = %s AND research_level = 'discovered';", (company_id,))


def set_candidate_stage(search_id, company_id, stage):
    """Analyst actions: shortlist or dismiss a candidate (or undo)."""
    with cursor() as cur:
        cur.execute(
            """
            UPDATE search_candidates
            SET stage = CASE WHEN %(stage)s = 'undo'
                             THEN CASE WHEN screened_at IS NULL THEN 'discovered' ELSE 'screened' END
                             ELSE %(stage)s END
            WHERE search_id = %(search_id)s AND company_id = %(company_id)s;
            """,
            {"stage": stage, "search_id": search_id, "company_id": company_id},
        )


# ---------------------------------------------------------------------------
# Assessments
# ---------------------------------------------------------------------------
# Append-only: the newest row per (search, company, dimension, criterion)
# is the current view, whether written by the AI or the analyst.

def save_ai_assessment(search_id, company_id, model, prompt_version, rows):
    """Store one AI assessment of a candidate in one transaction. rows:
    dicts with dimension, criterion, rating, rationale, facts_summary,
    interpretation, fact_ids, event_ids."""
    with cursor() as cur:
        returned = execute_values(
            cur,
            """
            INSERT INTO assessments (search_id, company_id, dimension, criterion, rating, rationale,
                                     facts_summary, interpretation, headline, highlights, cites_brief,
                                     assessed_by, model, prompt_version)
            VALUES %s
            RETURNING id, dimension, criterion;
            """,
            [(search_id, company_id, r["dimension"], r["criterion"], r["rating"], r.get("rationale"),
              r.get("facts_summary"), r.get("interpretation"), r.get("headline"),
              Json(r["highlights"]) if r.get("highlights") else None, bool(r.get("cites_brief")),
              model, prompt_version) for r in rows],
            template="(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'ai', %s, %s)",
            fetch=True,
        )
        ids = {(r["dimension"], r["criterion"]): r["id"] for r in returned}
        links = []
        for r in rows:
            assessment_id = ids[(r["dimension"], r["criterion"])]
            links += [(assessment_id, fid, None) for fid in r.get("fact_ids", [])]
            links += [(assessment_id, None, eid) for eid in r.get("event_ids", [])]
        if links:
            execute_values(cur, "INSERT INTO assessment_evidence (assessment_id, fact_id, event_id) VALUES %s;",
                           links, page_size=1000)


def insert_analyst_assessment(search_id, company_id, dimension, criterion, rating, rationale):
    """An analyst override. It becomes the current view; the AI draft stays."""
    with cursor() as cur:
        cur.execute(
            """
            INSERT INTO assessments (search_id, company_id, dimension, criterion, rating, rationale, assessed_by)
            VALUES (%s, %s, %s, %s, %s, %s, 'analyst');
            """,
            (search_id, company_id, dimension, criterion, rating, rationale),
        )


def get_ai_assessment(search_id, company_id):
    """The latest AI assessment of a candidate: {(dimension, criterion):
    row with fact_ids and event_ids it cites}."""
    with cursor(readonly=True) as cur:
        cur.execute(
            """
            SELECT a.*,
                   coalesce((SELECT array_agg(fact_id) FROM assessment_evidence
                             WHERE assessment_id = a.id AND fact_id IS NOT NULL), '{}') AS fact_ids,
                   coalesce((SELECT array_agg(event_id) FROM assessment_evidence
                             WHERE assessment_id = a.id AND event_id IS NOT NULL), '{}') AS event_ids
            FROM (SELECT DISTINCT ON (dimension, criterion) *
                  FROM assessments
                  WHERE search_id = %s AND company_id = %s AND assessed_by = 'ai'
                  ORDER BY dimension, criterion, created_at DESC) a;
            """,
            (search_id, company_id),
        )
        return {(r["dimension"], r["criterion"]): r for r in cur.fetchall()}


def get_assessments(search_id, company_ids):
    """Return {company_id: {(dimension, criterion): latest assessment}}."""
    if not company_ids:
        return {}
    with cursor(readonly=True) as cur:
        cur.execute(
            """
            SELECT DISTINCT ON (company_id, dimension, criterion) *
            FROM assessments
            WHERE search_id = %s AND company_id = ANY(%s)
            ORDER BY company_id, dimension, criterion, created_at DESC;
            """,
            (search_id, list(company_ids)),
        )
        rows = cur.fetchall()
    grouped = defaultdict(dict)
    for row in rows:
        grouped[row["company_id"]][(row["dimension"], row["criterion"])] = row
    return grouped
