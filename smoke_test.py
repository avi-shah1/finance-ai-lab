"""End-to-end check of every page and form, run against the real database
but inside ONE transaction that is rolled back at the end, so no test data
is ever saved.

Usage:  python smoke_test.py
"""

import re

import database

_real_conn = database.get_connection()


class _SharedConnection:
    """Stands in for a psycopg2 connection: every request reuses the same
    real connection, and commits/closes are ignored so the final rollback
    undoes everything."""

    def cursor(self, *args, **kwargs):
        return _real_conn.cursor(*args, **kwargs)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def close(self):
        pass


database.get_connection = lambda: _SharedConnection()

from app import app  # noqa: E402  (import after patching)

client = app.test_client()
failures = []


def check(label, condition):
    print(("PASS  " if condition else "FAIL  ") + label)
    if not condition:
        failures.append(label)


def text(resp):
    return resp.get_data(as_text=True)


try:
    r = client.get("/")
    check("home page loads", r.status_code == 200 and "Find Targets" in text(r))

    r = client.post("/searches", data={"buyer": "", "target_sector": "x", "geography": "y"},
                    follow_redirects=True)
    check("search requires all fields", "are all required" in text(r))

    r = client.post("/searches", data={"buyer": "Smoke Buyer Co", "target_sector": "Test SaaS",
                                       "geography": "Nowhere"})
    check("search created -> redirect", r.status_code == 302)
    search_url = r.headers["Location"]
    search_id = int(re.search(r"/searches/(\d+)", search_url).group(1))

    r = client.get(search_url)
    body = text(r)
    check("search page loads", r.status_code == 200 and "Smoke Buyer Co" in body)
    check("buyer shows not-researched state", "Buyer research has not yet been run" in body)

    r = client.post("/companies", data={
        "search_id": search_id, "name": "Smoke Target Ltd", "website": "example.com",
        "hq_country": "UK", "revenue": "£1.2m", "employee_count": "1,200",
        "source_type": "regulatory_filing", "source_name": "Companies House",
        "source_url": "https://example.com/filing", "evidence_type": "verified",
    }, follow_redirects=True)
    body = text(r)
    check("new candidate appears in table", "Smoke Target Ltd" in body and "£1.2m" in body)

    with database.cursor() as cur:
        cur.execute("SELECT id FROM companies WHERE name = 'Smoke Target Ltd';")
        target_id = cur.fetchone()["id"]
        cur.execute("SELECT value_number FROM facts WHERE company_id = %s AND fact_type = 'employee_count';",
                    (target_id,))
        check("employee count parsed to number", cur.fetchone()["value_number"] == 1200)

    r = client.post("/companies", data={
        "name": "Bad Source Co", "revenue": "1", "source_type": "news_article",
        "source_name": "X", "source_url": "javascript:alert(1)",
    }, follow_redirects=True)
    check("non-http source URL rejected", "must start with http" in text(r))

    r = client.post("/companies", data={
        "name": "Unverifiable Co", "revenue": "1", "evidence_type": "verified",
    }, follow_redirects=True)
    check("'verified' without URL rejected", "needs a source URL" in text(r))

    company_url = f"/companies/{target_id}?search={search_id}"
    r = client.post(f"/companies/{target_id}/facts", data={
        "next": company_url, "fact_type": "product", "value_text": "Widget Cloud",
        "excerpt": "Our flagship product", "source_type": "company_website",
        "source_name": "Company website", "source_url": "https://example.com/products",
        "evidence_type": "company_reported",
    }, follow_redirects=True)
    body = text(r)
    check("fact recorded and shown with excerpt", "Widget Cloud" in body and "Our flagship product" in body)
    check("source link is clickable", 'href="https://example.com/products"' in body)

    r = client.post(f"/companies/{target_id}/events", data={
        "next": company_url, "event_type": "fundraising", "title": "Raised a round",
        "event_date": "2026-01-15", "source_type": "news_article", "source_name": "Example News",
    }, follow_redirects=True)
    check("event recorded and shown", "Raised a round" in text(r))

    r = client.post(f"/companies/{target_id}/events", data={
        "next": "https://evil.example/", "event_type": "other", "title": "x",
    })
    check("external `next` redirect refused", r.headers["Location"].startswith("/companies/"))

    with database.cursor() as cur:
        cur.execute("""INSERT INTO assessments (search_id, company_id, dimension, rating, assessed_by)
                       VALUES (%s, %s, 'overall', 'good_fit', 'analyst');""", (search_id, target_id))
    r = client.get(search_url)
    check("screening view renders when an assessment exists", "Good Fit" in text(r))

    r = client.get(f"/companies/{target_id}")
    check("company page without search context loads", r.status_code == 200
          and "Open this company from a search" in text(r))

    with database.cursor() as cur:
        cur.execute("SELECT buyer_company_id FROM searches WHERE id = %s;", (search_id,))
        buyer_id = cur.fetchone()["buyer_company_id"]
    r = client.get(f"/buyers/{buyer_id}")
    body = text(r)
    check("buyer profile page loads with empty states",
          r.status_code == 200 and "Research has not yet been run" in body)

    r = client.post(f"/searches/{search_id}/candidates/{target_id}/remove", follow_redirects=True)
    check("candidate removed", "Smoke Target Ltd" not in text(r).split("Add from company library")[0])

    check("unknown company -> 404", client.get("/companies/999999").status_code == 404)
    check("unknown search -> 404", client.get("/searches/999999").status_code == 404)

    try:
        with database.cursor() as cur:
            cur.execute("SAVEPOINT provenance_check;")
            cur.execute("""INSERT INTO facts (company_id, fact_type, value_text, evidence_type)
                           VALUES (%s, 'revenue', '1', 'verified');""", (target_id,))
        check("database rejects a fact with no source", False)
    except Exception:
        _real_conn.cursor().execute("ROLLBACK TO SAVEPOINT provenance_check;")
        check("database rejects a fact with no source", True)
finally:
    _real_conn.rollback()
    _real_conn.close()
    print("\nRolled back: no test data was saved.")

print(f"\n{len(failures)} failure(s)" if failures else "\nAll checks passed.")
raise SystemExit(1 if failures else 0)
