"""Write a ProviderResult into Postgres, in one transaction.

Re-running a provider is safe: each source is identified by
(company, provider, external_id), so a second run updates the source's
retrieved_at and replaces the facts it supports, rather than piling up
duplicates. Facts from *other* providers are never touched, which is how
a weaker source can never overwrite a stronger one.
"""

import re

from psycopg2.extras import Json, execute_values

import database

# How each provider's facts were obtained, unless the fact says otherwise.
DEFAULT_METHOD = {
    "companies_house": "structured_api",
    "sec": "structured_api",
    "yfinance": "third_party_api",
    "website": "html_rule",
    "ai_documents": "llm_document",
    "ai_website": "llm_website",
    "discovery": "llm_website",
}


def save_result(company_id, provider, result):
    """Returns the number of facts stored."""
    count = 0
    with database.cursor() as cur:
        kept_ids = []
        for src in result.sources:
            source_id = _upsert_source(cur, company_id, provider, src)
            kept_ids.append(source_id)
            # Replace only this provider's facts: AI facts extracted from the
            # same document are left alone.
            cur.execute("DELETE FROM facts WHERE source_id = %s AND extracted_by = %s;",
                        (source_id, provider))
            for fact in src.facts:
                _insert_fact(cur, company_id, source_id, fact, provider)
                count += 1

        for source_id, item in result.attach.items():
            cur.execute("DELETE FROM facts WHERE source_id = %s AND extracted_by = %s;",
                        (source_id, provider))
            for fact in item["facts"]:
                _insert_fact(cur, company_id, source_id, fact, provider)
                count += 1
            cur.execute("UPDATE sources SET ai_extracted_sha256 = %s WHERE id = %s;",
                        (item.get("content_sha256"), source_id))

        # On a clean run, drop this provider's sources that no longer exist
        # (e.g. a web page that has been removed). Partial runs keep them.
        if result.status == "success" and result.sources:
            cur.execute(
                """
                DELETE FROM facts WHERE source_id IN (
                    SELECT id FROM sources
                    WHERE company_id = %s AND provider = %s AND NOT (id = ANY(%s)));
                DELETE FROM sources
                WHERE company_id = %s AND provider = %s AND NOT (id = ANY(%s))
                  AND NOT EXISTS (SELECT 1 FROM events e WHERE e.source_id = sources.id);
                """,
                (company_id, provider, kept_ids or [-1], company_id, provider, kept_ids or [-1]),
            )

        for scheme, (value, method) in result.identifiers.items():
            # Never replace an identifier the analyst typed in.
            cur.execute(
                """
                INSERT INTO company_identifiers (company_id, scheme, value, match_method)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (company_id, scheme) DO UPDATE
                    SET value = EXCLUDED.value, match_method = EXCLUDED.match_method
                    WHERE company_identifiers.match_method <> 'analyst_entered';
                """,
                (company_id, scheme, str(value), method),
            )

        if result.website and _is_http(result.website):
            cur.execute(
                "UPDATE companies SET website = %s WHERE id = %s AND coalesce(website, '') = '';",
                (result.website, company_id),
            )
    return count


def save_sources_bulk(provider, items):
    """Save many (company_id, SourceDoc) pairs from one provider in three
    queries, however many companies: upsert every source, clear this
    provider's facts on them, insert the new facts. For discovery, where
    round trips to the hosted database, not data volume, are the cost.
    Other sources of the same companies are left alone (no pruning)."""
    items = [(cid, src) for cid, src in items if src.external_id]
    if not items:
        return 0
    with database.cursor() as cur:
        returned = execute_values(
            cur,
            """
            INSERT INTO sources (company_id, provider, external_id, source_type, name, title, url,
                                 published_on, notes)
            VALUES %s
            ON CONFLICT (company_id, provider, external_id) WHERE external_id IS NOT NULL
            DO UPDATE SET source_type = EXCLUDED.source_type, name = EXCLUDED.name,
                          title = EXCLUDED.title, url = EXCLUDED.url, notes = EXCLUDED.notes,
                          retrieved_at = now()
            RETURNING id, company_id, external_id;
            """,
            [(cid, provider, src.external_id, src.source_type, src.name, src.title,
              src.url if _is_http(src.url) else None, src.published_on, src.notes) for cid, src in items],
            fetch=True,
        )
        ids = {(r["company_id"], r["external_id"]): r["id"] for r in returned}
        cur.execute("DELETE FROM facts WHERE source_id = ANY(%s) AND extracted_by = %s;",
                    (list(ids.values()), provider))
        rows = [
            (cid, ids[(cid, src.external_id)], f.fact_type, f.value_text, f.value_number, f.currency,
             f.period, f.period_start, f.period_end, f.as_of, f.evidence_type, f.confidence,
             f.excerpt, f.location, f.notes, provider, f.extraction_method or DEFAULT_METHOD.get(provider),
             f.extraction_model, f.verification, f.evidence_page)
            for cid, src in items for f in src.facts
        ]
        if rows:
            execute_values(
                cur,
                """
                INSERT INTO facts (company_id, source_id, fact_type, value_text, value_number, currency,
                                   period, period_start, period_end, as_of, evidence_type, confidence,
                                   excerpt, location, notes, extracted_by, extraction_method,
                                   extraction_model, verification, evidence_page)
                VALUES %s;
                """,
                rows,
                page_size=500,
            )
    return len(rows)


def _upsert_source(cur, company_id, provider, src):
    cur.execute(
        """
        INSERT INTO sources (company_id, provider, external_id, source_type, name, title, url,
                             published_on, document_type, period_start, period_end,
                             parse_status, notes, content_path, content_sha256)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (company_id, provider, external_id) WHERE external_id IS NOT NULL
        DO UPDATE SET source_type = EXCLUDED.source_type, name = EXCLUDED.name,
                      title = EXCLUDED.title, url = EXCLUDED.url,
                      published_on = EXCLUDED.published_on, document_type = EXCLUDED.document_type,
                      period_start = EXCLUDED.period_start, period_end = EXCLUDED.period_end,
                      parse_status = EXCLUDED.parse_status, notes = EXCLUDED.notes,
                      content_path = coalesce(EXCLUDED.content_path, sources.content_path),
                      content_sha256 = coalesce(EXCLUDED.content_sha256, sources.content_sha256),
                      retrieved_at = now()
        RETURNING id;
        """,
        (company_id, provider, src.external_id, src.source_type, src.name, src.title,
         src.url if _is_http(src.url) else None, src.published_on, src.document_type,
         src.period_start, src.period_end, src.parse_status, src.notes,
         src.content_path, src.content_sha256),
    )
    return cur.fetchone()["id"]


def _insert_fact(cur, company_id, source_id, f, provider):
    cur.execute(
        """
        INSERT INTO facts (company_id, source_id, fact_type, value_text, value_number, currency,
                           period, period_start, period_end, as_of, evidence_type, confidence,
                           excerpt, location, notes, extracted_by, extraction_method,
                           extraction_model, verification, evidence_page)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s);
        """,
        (company_id, source_id, f.fact_type, f.value_text, f.value_number, f.currency,
         f.period, f.period_start, f.period_end, f.as_of, f.evidence_type, f.confidence,
         f.excerpt, f.location, f.notes, provider,
         f.extraction_method or DEFAULT_METHOD.get(provider), f.extraction_model,
         f.verification, f.evidence_page),
    )


def start_run(company_id, provider):
    with database.cursor() as cur:
        cur.execute(
            "INSERT INTO research_runs (company_id, provider, status) VALUES (%s, %s, 'running') RETURNING id;",
            (company_id, provider),
        )
        return cur.fetchone()["id"]


def finish_run(run_id, status, message, detail, facts_added):
    with database.cursor() as cur:
        cur.execute(
            """
            UPDATE research_runs
            SET status = %s, message = %s, detail = %s, facts_added = %s, finished_at = now()
            WHERE id = %s;
            """,
            (status, message, Json(detail or {}), facts_added, run_id),
        )


def _is_http(url):
    return bool(url) and re.match(r"^https?://", url, re.I) is not None
