import os
import re
from datetime import date
from decimal import Decimal, InvalidOperation

from flask import Flask, abort, flash, redirect, render_template, request, url_for

import database
import jobs
import screening
from enrichment.runner import AI_LABELS, PROVIDER_LABELS, STRUCTURED_LABELS, enrich_company
from profiles import CompanyProfile, build_profiles

app = Flask(__name__)
# Needed for flash messages. Set FLASK_SECRET_KEY in .env to keep messages
# working across restarts; the random fallback is fine for local use.
app.secret_key = os.environ.get("FLASK_SECRET_KEY") or os.urandom(24)


# ---------------------------------------------------------------------------
# Template helpers
# ---------------------------------------------------------------------------

@app.context_processor
def inject_vocabulary():
    """Make the screening vocabulary available in every template."""
    return {"vocab": screening, "provider_labels": PROVIDER_LABELS,
            "structured_labels": STRUCTURED_LABELS, "ai_labels": AI_LABELS}


@app.template_filter("external_url")
def external_url(value):
    """Return a safe http(s) link for a stored URL/website, or None.
    Bare domains like 'acme.com' become https://acme.com; anything with a
    different scheme (e.g. javascript:) is refused."""
    if not value:
        return None
    value = value.strip()
    if re.match(r"^https?://", value, re.I):
        return value
    if re.match(r"^[\w.-]+\.[a-z]{2,}(/\S*)?$", value, re.I):
        return "https://" + value
    return None


@app.template_filter("fmt_date")
def fmt_date(value):
    return value.strftime("%d %b %Y") if value else ""


@app.template_filter("fmt_datetime")
def fmt_datetime(value):
    return value.astimezone().strftime("%d %b %Y, %H:%M") if value else ""


# ---------------------------------------------------------------------------
# Form parsing
# ---------------------------------------------------------------------------

class FormError(ValueError):
    pass


def _text(name):
    return (request.form.get(name) or "").strip() or None


def _date(name):
    raw = _text(name)
    if raw is None:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        raise FormError(f"'{raw}' is not a valid date.")


def _number(raw):
    if raw is None:
        return None
    try:
        return Decimal(raw.replace(",", ""))
    except InvalidOperation:
        return None


def read_source_form():
    """Read the shared 'Evidence source' fields (see _components.html)."""
    source_type = _text("source_type") or "manual_entry"
    evidence_type = _text("evidence_type") or "analyst_entry"
    name = _text("source_name")
    url = _text("source_url")

    if source_type not in screening.SOURCE_TYPES:
        raise FormError("Unknown source type.")
    if evidence_type not in screening.MANUAL_EVIDENCE_TYPES:
        raise FormError("Unknown evidence type.")
    if url and not re.match(r"^https?://", url, re.I):
        raise FormError("Source URL must start with http:// or https://")
    if source_type == "manual_entry" and not name:
        name = "Analyst entry"
    if not name:
        raise FormError("Please name the source (e.g. Companies House).")
    if evidence_type == "verified" and not url:
        raise FormError("'Verified' evidence needs a source URL so it can be checked.")

    source = {
        "source_type": source_type,
        "name": name,
        "url": url,
        "title": _text("source_title"),
        "published_on": _date("source_published_on"),
    }
    return source, evidence_type


def _next_url(default):
    """Where to go after a form post. Only same-site paths are allowed, so the
    hidden `next` field can't be used to redirect somewhere else."""
    target = request.form.get("next") or ""
    return target if target.startswith("/") and not target.startswith("//") else default


def form_error_redirect(error, target):
    flash(str(error), "error")
    return redirect(target)


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

@app.route("/")
def home():
    companies = database.get_all_companies(library_only=True)
    ids = [c["id"] for c in companies]
    profiles = build_profiles(companies, database.get_facts_for(ids), database.get_events_for(ids),
                              database.get_latest_runs(ids))
    return render_template(
        "index.html",
        profiles=profiles,
        searches=database.get_recent_searches(),
    )


@app.route("/searches", methods=["POST"])
def create_search():
    buyer, sector, geography = _text("buyer"), _text("target_sector"), _text("geography")
    if not (buyer and sector and geography):
        return form_error_redirect("Buyer, target sector and geography are all required.",
                                   url_for("home"))
    buyer_id = database.find_or_create_company(buyer)
    search_id = database.insert_search(buyer_id, sector, geography, _text("criteria"))
    jobs.start_search(search_id)
    return redirect(url_for("show_search", search_id=search_id))


# Search statuses during which the page refreshes itself to show progress.
RUNNING_STATUSES = ("queued", "discovering", "screening")


@app.route("/searches/<int:search_id>")
def show_search(search_id):
    search = database.get_search(search_id) or abort(404)
    candidates = database.get_candidates(search_id)
    ids = [c["id"] for c in candidates]
    profiles = build_profiles(candidates, database.get_facts_for(ids), database.get_events_for(ids),
                              database.get_latest_runs(ids))
    shown = [p for p in profiles if p.company["stage"] not in ("filtered_out", "dismissed")]
    filtered = [p for p in profiles if p.company["stage"] == "filtered_out"]

    buyer_id = search["buyer_company_id"]
    buyer = _load_profile(buyer_id)

    # Library companies that could still be added: not the buyer, not already listed.
    excluded = set(ids) | {buyer_id}
    addable = [c for c in database.get_all_companies(library_only=True) if c["id"] not in excluded]

    usage = database.get_usage_summary(search_id=search_id)
    return render_template(
        "search.html",
        search=search,
        running=search["status"] in RUNNING_STATUSES,
        buyer=buyer,
        profiles=shown,
        filtered=filtered,
        assessments=database.get_assessments(search_id, ids),
        addable=addable,
        usage=usage,
        total_cost=sum((u["cost_usd"] or 0) for u in usage),
    )


@app.route("/searches/<int:search_id>/run", methods=["POST"])
def rerun_search(search_id):
    """Run discovery again, replacing this search's previous discovered results."""
    search = database.get_search(search_id) or abort(404)
    if search["status"] in RUNNING_STATUSES and jobs.is_running(search_id):
        flash("This search is already running.", "info")
    else:
        jobs.start_search(search_id)
    return redirect(url_for("show_search", search_id=search_id))


@app.route("/searches/<int:search_id>/candidates", methods=["POST"])
def add_candidate(search_id):
    company_id = request.form.get("company_id", type=int)
    if company_id:
        database.add_candidate(search_id, company_id)
    return redirect(url_for("show_search", search_id=search_id))


@app.route("/searches/<int:search_id>/candidates/<int:company_id>/remove", methods=["POST"])
def remove_candidate(search_id, company_id):
    database.remove_candidate(search_id, company_id)
    return redirect(url_for("show_search", search_id=search_id))


# Quick facts the "add company" form can capture alongside identity fields.
QUICK_FACTS = ["description", "revenue", "employee_count", "ownership_status"]


@app.route("/companies", methods=["POST"])
def add_company():
    search_id = request.form.get("search_id", type=int)
    back = url_for("show_search", search_id=search_id) if search_id else url_for("home")

    name = _text("name")
    if not name:
        return form_error_redirect("Company name is required.", back)

    facts = []
    for fact_type in QUICK_FACTS:
        value = _text(fact_type)
        if value:
            facts.append({
                "fact_type": fact_type,
                "value_text": value,
                "value_number": _number(value) if fact_type == "employee_count" else None,
            })

    source = None
    if facts:
        try:
            source, evidence_type = read_source_form()
        except FormError as error:
            return form_error_redirect(error, back)
        for fact in facts:
            fact["evidence_type"] = evidence_type

    database.create_company(
        name=name,
        website=_text("website"),
        hq_country=_text("hq_country"),
        sector=_text("sector"),
        source=source,
        facts=facts,
        search_id=search_id,
    )
    return redirect(back)


def _load_profile(company_id):
    company = database.get_company(company_id) or abort(404)
    return CompanyProfile(
        company,
        database.get_facts_for([company_id]).get(company_id, []),
        database.get_events_for([company_id]).get(company_id, []),
        runs=database.get_latest_runs([company_id]).get(company_id, {}),
        sources=database.get_sources(company_id),
        identifiers=database.get_identifiers(company_id, detailed=True),
    )


@app.route("/companies/<int:company_id>")
def show_company(company_id):
    profile = _load_profile(company_id)

    # Assessments only make sense against a particular buyer, i.e. a search.
    search = None
    assessments = {}
    search_id = request.args.get("search", type=int)
    if search_id:
        search = database.get_search(search_id)
        if search:
            assessments = database.get_assessments(search_id, [company_id]).get(company_id, {})

    return render_template(
        "company.html",
        profile=profile,
        search=search,
        assessments=assessments,
        fact_types=database.get_fact_types(),
    )


@app.route("/buyers/<int:company_id>")
def show_buyer(company_id):
    return render_template(
        "buyer.html",
        profile=_load_profile(company_id),
        searches=database.get_searches_for_buyer(company_id),
    )


@app.route("/companies/<int:company_id>/facts", methods=["POST"])
def add_fact(company_id):
    back = _next_url(url_for("show_company", company_id=company_id))
    fact_types = {ft["code"]: ft for ft in database.get_fact_types()}
    try:
        fact_type = fact_types.get(_text("fact_type")) or abort(400)
        value_text = _text("value_text")
        if not value_text:
            raise FormError("Please enter the value.")
        value_number = _number(_text("value_number"))
        if value_number is None and fact_type["value_kind"] == "number":
            value_number = _number(value_text)
        currency = _text("currency")
        if currency and not re.fullmatch(r"[A-Za-z]{3}", currency):
            raise FormError("Currency must be a 3-letter code such as GBP or USD.")
        source, evidence_type = read_source_form()
        fact = {
            "fact_type": fact_type["code"],
            "value_text": value_text,
            "value_number": value_number,
            "currency": currency.upper() if currency else None,
            "period": _text("period"),
            "as_of": _date("as_of"),
            "excerpt": _text("excerpt"),
            "evidence_type": evidence_type,
        }
    except FormError as error:
        return form_error_redirect(error, back)

    database.record_facts(company_id, source, [fact])
    flash(f"{fact_type['label']} recorded.", "info")
    return redirect(back)


@app.route("/companies/<int:company_id>/events", methods=["POST"])
def add_event(company_id):
    back = _next_url(url_for("show_company", company_id=company_id))
    try:
        event_type = _text("event_type")
        if event_type not in screening.EVENT_TYPES:
            raise FormError("Unknown event type.")
        title = _text("title")
        if not title:
            raise FormError("Please give the event a title.")
        source, evidence_type = read_source_form()
        event = {
            "event_type": event_type,
            "title": title,
            "summary": _text("summary"),
            "event_date": _date("event_date"),
            "evidence_type": evidence_type,
        }
    except FormError as error:
        return form_error_redirect(error, back)

    database.record_event(company_id, source, event)
    flash("Event recorded.", "info")
    return redirect(back)


# ---------------------------------------------------------------------------
# Enrichment
# ---------------------------------------------------------------------------

@app.route("/companies/<int:company_id>/identifiers", methods=["POST"])
def update_identifiers(company_id):
    """Identity details that tell the providers exactly which company to
    research. Entered values are trusted over automatic name matches."""
    back = _next_url(url_for("show_company", company_id=company_id))
    database.get_company(company_id) or abort(404)

    number = (_text("companies_house") or "").upper().replace(" ", "")
    if number and not re.fullmatch(r"[A-Z0-9]{8}|\d{1,8}", number):
        return form_error_redirect("Companies House numbers are 8 characters, e.g. 01234567 or SC123456.", back)
    if number.isdigit():
        number = number.zfill(8)
    cik = (_text("sec_cik") or "").lstrip("0")
    if cik and not cik.isdigit():
        return form_error_redirect("An SEC CIK is a number, e.g. 1108524.", back)
    ticker = (_text("ticker") or "").upper()
    if ticker and not re.fullmatch(r"[A-Z0-9.\-]{1,12}", ticker):
        return form_error_redirect("That doesn't look like a stock ticker (e.g. CRM or VOD.L).", back)
    website = _text("website")
    if website and external_url(website) is None:
        return form_error_redirect("Website should look like example.com or https://example.com.", back)

    database.update_company_identity(company_id, website, _text("hq_country"), _text("sector"))
    database.set_identifier(company_id, "companies_house", number)
    database.set_identifier(company_id, "sec_cik", cik)
    database.set_identifier(company_id, "ticker", ticker)
    flash("Identity details saved. Run research to fetch sources.", "info")
    return redirect(back)


@app.route("/companies/<int:company_id>/research", methods=["POST"])
def run_research(company_id):
    """Fetch evidence from every applicable source. Runs in the request, so
    it takes a little while (usually 5–40 seconds)."""
    back = _next_url(url_for("show_company", company_id=company_id))
    database.get_company(company_id) or abort(404)
    summaries = enrich_company(company_id)
    stored = sum(s["facts"] for s in summaries)
    ran = [s for s in summaries if s["status"] != "not_applicable"]
    problems = [f"{s['label']}: {screening.RUN_STATUSES[s['status']][0]}" for s in ran
                if s["status"] not in ("success", "partial")]
    flash(f"Research finished: {stored} facts stored from {len(ran)} provider(s)."
          + (f" Issues: {'; '.join(problems)}." if problems else ""), "info" if not problems else "error")
    return redirect(back)


@app.route("/companies/<int:company_id>/ai-extract", methods=["POST"])
def run_ai_extraction(company_id):
    """Ask Claude to extract facts from documents and pages already
    retrieved for this company. Separate from research because each call
    costs money; unchanged documents are skipped automatically."""
    back = _next_url(url_for("show_company", company_id=company_id))
    database.get_company(company_id) or abort(404)
    summaries = enrich_company(company_id, group="ai")
    stored = sum(s["facts"] for s in summaries)
    ran = [s for s in summaries if s["status"] != "not_applicable"]
    problems = [f"{s['label']}: {s['message']}" for s in ran if s["status"] not in ("success", "partial")]
    if not ran:
        flash("Nothing to extract yet: run research first to retrieve PDFs and web pages.", "error")
    else:
        flash(f"AI extraction finished: {stored} verified facts stored."
              + (f" Issues: {'; '.join(problems)}" if problems else ""), "error" if problems else "info")
    return redirect(back)


@app.errorhandler(404)
def not_found(error):
    return render_template("404.html"), 404


if __name__ == "__main__":
    jobs.fail_interrupted()
    app.run(debug=True)
