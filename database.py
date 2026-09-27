"""All SQL lives here. Routes in app.py call these functions and never write SQL."""

import os
from collections import defaultdict
from contextlib import contextmanager

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.environ["DATABASE_URL"]


def get_connection():
    return psycopg2.connect(DATABASE_URL)


@contextmanager
def cursor():
    """Open a connection, yield a cursor that returns rows as dicts, commit on
    success and roll back if anything inside the `with` block raises."""
    conn = get_connection()
    try:
        with conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                yield cur
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Companies
# ---------------------------------------------------------------------------

def get_all_companies():
    with cursor() as cur:
        cur.execute("SELECT * FROM companies ORDER BY name;")
        return cur.fetchall()


def get_company(company_id):
    with cursor() as cur:
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
    with cursor() as cur:
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

def _insert_source(cur, source):
    cur.execute(
        """
        INSERT INTO sources (source_type, name, url, title, published_on)
        VALUES (%(source_type)s, %(name)s, %(url)s, %(title)s, %(published_on)s)
        RETURNING id;
        """,
        source,
    )
    return cur.fetchone()["id"]


def _insert_fact(cur, company_id, source_id, fact):
    cur.execute(
        """
        INSERT INTO facts (company_id, fact_type, value_text, value_number, currency,
                           period, as_of, source_id, evidence_type, excerpt)
        VALUES (%(company_id)s, %(fact_type)s, %(value_text)s, %(value_number)s, %(currency)s,
                %(period)s, %(as_of)s, %(source_id)s, %(evidence_type)s, %(excerpt)s);
        """,
        {"value_number": None, "currency": None, "period": None, "as_of": None, "excerpt": None,
         **fact, "company_id": company_id, "source_id": source_id},
    )


def record_facts(company_id, source, facts):
    """Save one source and the facts it supports."""
    with cursor() as cur:
        source_id = _insert_source(cur, source)
        for fact in facts:
            _insert_fact(cur, company_id, source_id, fact)


def record_event(company_id, source, event):
    with cursor() as cur:
        source_id = _insert_source(cur, source)
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
    """Create a company, optionally with sourced facts, and optionally add it
    to a search's candidate list, all in one transaction."""
    with cursor() as cur:
        cur.execute(
            """
            INSERT INTO companies (name, website, hq_country, sector)
            VALUES (%s, %s, %s, %s)
            RETURNING id;
            """,
            (name, website, hq_country, sector),
        )
        company_id = cur.fetchone()["id"]
        if facts:
            source_id = _insert_source(cur, source)
            for fact in facts:
                _insert_fact(cur, company_id, source_id, fact)
        if search_id is not None:
            cur.execute(
                "INSERT INTO search_candidates (search_id, company_id) VALUES (%s, %s);",
                (search_id, company_id),
            )
        return company_id


# Columns every fact/event query returns about its source, so templates can
# render the same source component everywhere.
_SOURCE_COLUMNS = """
    s.id AS source_id, s.source_type, s.name AS source_name, s.title AS source_title,
    s.url AS source_url, s.published_on AS source_published_on,
    s.retrieved_at AS source_retrieved_at
"""


def get_facts_for(company_ids):
    """Return {company_id: [fact, ...]} with source details joined on.
    Within each company, the most recent value of a fact type comes first."""
    if not company_ids:
        return {}
    with cursor() as cur:
        cur.execute(
            f"""
            SELECT f.*, ft.label, ft.category, ft.value_kind, ft.multi_valued, {_SOURCE_COLUMNS}
            FROM facts f
            JOIN fact_types ft ON ft.code = f.fact_type
            JOIN sources s ON s.id = f.source_id
            WHERE f.company_id = ANY(%s)
            ORDER BY ft.sort_order, f.as_of DESC NULLS LAST, f.created_at DESC;
            """,
            (list(company_ids),),
        )
        rows = cur.fetchall()
    grouped = defaultdict(list)
    for row in rows:
        grouped[row["company_id"]].append(row)
    return grouped


def get_events_for(company_ids):
    """Return {company_id: [event, ...]} newest first, with source details."""
    if not company_ids:
        return {}
    with cursor() as cur:
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


# ---------------------------------------------------------------------------
# Searches and candidates
# ---------------------------------------------------------------------------

def insert_search(buyer_company_id, target_sector, geography):
    with cursor() as cur:
        cur.execute(
            """
            INSERT INTO searches (buyer_company_id, target_sector, geography)
            VALUES (%s, %s, %s)
            RETURNING id;
            """,
            (buyer_company_id, target_sector, geography),
        )
        return cur.fetchone()["id"]


_SEARCH_SELECT = """
    SELECT s.*, b.name AS buyer_name,
           (SELECT count(*) FROM search_candidates sc WHERE sc.search_id = s.id) AS candidate_count
    FROM searches s
    JOIN companies b ON b.id = s.buyer_company_id
"""


def get_search(search_id):
    with cursor() as cur:
        cur.execute(_SEARCH_SELECT + " WHERE s.id = %s;", (search_id,))
        return cur.fetchone()


def get_recent_searches(limit=10):
    with cursor() as cur:
        cur.execute(_SEARCH_SELECT + " ORDER BY s.created_at DESC LIMIT %s;", (limit,))
        return cur.fetchall()


def get_searches_for_buyer(buyer_company_id):
    with cursor() as cur:
        cur.execute(_SEARCH_SELECT + " WHERE s.buyer_company_id = %s ORDER BY s.created_at DESC;",
                    (buyer_company_id,))
        return cur.fetchall()


def get_candidates(search_id):
    with cursor() as cur:
        cur.execute(
            """
            SELECT c.*, sc.origin, sc.added_at
            FROM search_candidates sc
            JOIN companies c ON c.id = sc.company_id
            WHERE sc.search_id = %s
            ORDER BY c.name;
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
# Assessments (read-only for now: nothing creates them yet)
# ---------------------------------------------------------------------------

def get_assessments(search_id, company_ids):
    """Return {company_id: {(dimension, criterion): latest assessment}}."""
    if not company_ids:
        return {}
    with cursor() as cur:
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
