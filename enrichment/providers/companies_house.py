"""Companies House: the official UK register.

API docs: https://developer-specs.company-information.service.gov.uk/
Auth:     HTTP basic auth with the API key as the username, blank password.
Limit:    600 requests per 5 minutes per key.

What this provider fetches, in order:
  1. company profile          -> identity, status, SIC codes, filing dates
  2. officers                 -> current directors/secretaries
  3. persons with significant control
  4. filing history (accounts category)
  5. for the most recent accounts filings: the Document API
       metadata -> which formats exist (iXBRL and/or PDF)
       content  -> download; parse iXBRL deterministically (see ixbrl.py)
                   or, for PDF-only filings, check the text layer and record
                   that automated extraction isn't available yet.
"""

import io
import os
import re
from datetime import date
from decimal import Decimal

from .. import document_cache, formatting, ixbrl
from ..base import (Ambiguous, CredentialsMissing, EnrichmentError, Fact, NotFound,
                    Provider, ProviderResult, SourceDoc)

API = "https://api.company-information.service.gov.uk"
DOC_API = "https://document-api.company-information.service.gov.uk"
PUBLIC = "https://find-and-update.company-information.service.gov.uk"

MAX_ACCOUNTS_DOCUMENTS = 3        # most recent accounts filings to download

UK_NAMES = {"uk", "united kingdom", "gb", "great britain", "england", "scotland", "wales",
            "northern ireland", "england and wales"}

COMPANY_TYPES = {
    "ltd": "Private limited company",
    "plc": "Public limited company",
    "llp": "Limited liability partnership",
    "private-unlimited": "Private unlimited company",
    "private-limited-guarant-nsc": "Private company limited by guarantee",
    "private-limited-guarant-nsc-limited-exemption": "Private company limited by guarantee",
    "limited-partnership": "Limited partnership",
    "uk-establishment": "UK establishment of an overseas company",
}

# Financial facts we look for in every parsed set of accounts. Any that the
# latest accounts don't contain are reported as "not disclosed".
HEADLINE_FINANCIALS = ["revenue", "operating_profit", "profit_before_tax", "net_income",
                       "cash", "net_assets", "employee_count"]


def is_uk(country):
    return (country or "").strip().lower() in UK_NAMES


class CompaniesHouse(Provider):
    name = "companies_house"
    label = "Companies House"

    def not_applicable_reason(self, ctx):
        if ctx.identifiers.get("companies_house") or is_uk(ctx.company.get("hq_country")):
            return None
        return ("Companies House covers UK companies. Set HQ country to UK "
                "or add a company number to use it.")

    def run(self, ctx):
        key = os.environ.get("COMPANIES_HOUSE_API_KEY")
        if not key:
            raise CredentialsMissing("API credentials required: add COMPANIES_HOUSE_API_KEY to .env.")
        self.http = ctx.http
        self.auth = (key, "")

        result = ProviderResult()
        number = ctx.identifiers.get("companies_house")
        if not number:
            number = self._match_by_name(ctx.company["name"])
            result.identifiers["companies_house"] = (number, "auto_exact_name")

        profile = self._get(f"/company/{number}")
        result.sources.append(self._profile_source(number, profile))

        # Each remaining step is independent: a failure is recorded and the
        # rest still run.
        problems = []
        for step in (self._officers_source, self._psc_source):
            try:
                result.sources.append(step(number))
            except EnrichmentError as exc:
                problems.append(str(exc))

        try:
            filings = self._accounts_filings(number)
        except EnrichmentError as exc:
            filings = []
            problems.append(f"Filing history: {exc}")

        latest_parsed = None
        for filing in filings[:MAX_ACCOUNTS_DOCUMENTS]:
            doc = self._accounts_document(number, filing)
            result.sources.append(doc)
            if doc.parse_status == "failed":
                problems.append(f"{doc.title}: {doc.notes}")
            if latest_parsed is None and doc.parse_status in ("parsed", "not_parsed", "unreadable"):
                latest_parsed = doc

        result.detail = self._disclosure_detail(latest_parsed, filings)
        if not filings:
            result.detail.setdefault("notes", []).append("No accounts filings found.")
        if problems:
            result.status = "partial"
            result.message = "; ".join(problems)
        facts = sum(len(s.facts) for s in result.sources)
        result.message = result.message or (
            f"{profile.get('company_name')} ({number}): {facts} facts from "
            f"{len(result.sources)} sources, {min(len(filings), MAX_ACCOUNTS_DOCUMENTS)} accounts filings checked.")
        return result

    # -- HTTP -------------------------------------------------------------

    def _get(self, path, **params):
        return self.http.get_json(API + path, params=params or None, auth=self.auth)

    # -- Matching ---------------------------------------------------------

    def _match_by_name(self, name):
        """Only accept a match when exactly one register entry has exactly
        this name (ignoring 'Ltd'/'Limited' etc.). Anything looser risks
        attaching another company's filings, so the analyst must confirm by
        entering the company number instead."""
        data = self._get("/search/companies", q=name, items_per_page=20)
        items = data.get("items") or []
        target = formatting.normalise_name(name)
        exact = [i for i in items if formatting.normalise_name(i.get("title")) == target]
        active = [i for i in exact if i.get("company_status") == "active"]
        pool = active or exact
        candidates = [{"name": i.get("title"), "number": i.get("company_number"),
                       "status": i.get("company_status"), "address": i.get("address_snippet")}
                      for i in (exact or items)[:5]]
        if len(pool) == 1:
            return pool[0]["company_number"]
        if not pool:
            raise NotFound(f"No Companies House company named exactly '{name}'. "
                           "Add the company number if you know it.", {"candidates": candidates})
        raise Ambiguous(f"{len(pool)} Companies House companies are named '{name}'. "
                        "Add the company number to choose one.", {"candidates": candidates})

    # -- Profile ----------------------------------------------------------

    def _profile_source(self, number, p):
        src = SourceDoc(
            provider=self.name, external_id=f"profile:{number}", source_type="regulatory_filing",
            name="Companies House", title="Company profile (register)",
            url=f"{PUBLIC}/company/{number}",
        )

        def add(fact_type, value, **kw):
            if value not in (None, "", []):
                src.facts.append(Fact(fact_type=fact_type, value_text=str(value),
                                      evidence_type="verified", confidence="high",
                                      location="Companies House company profile", **kw))

        add("legal_name", p.get("company_name"))
        add("company_number", p.get("company_number"))
        status = p.get("company_status")
        if status:
            detail = p.get("company_status_detail")
            add("legal_status", status.replace("-", " ").capitalize()
                + (f" ({detail.replace('-', ' ')})" if detail else ""))
        if p.get("type"):
            add("company_type", COMPANY_TYPES.get(p["type"], p["type"].replace("-", " ").capitalize()))
        if p.get("date_of_creation"):
            add("incorporation_date", formatting.day(p["date_of_creation"]),
                as_of=formatting.parse_date(p["date_of_creation"]))
        add("registered_office", _address(p.get("registered_office_address")))
        for sic in p.get("sic_codes") or []:
            add("sic_code", sic)

        accounts = p.get("accounts") or {}
        last = accounts.get("last_accounts") or {}
        if last.get("made_up_to"):
            add("last_accounts_made_up_to", formatting.day(last["made_up_to"]),
                as_of=formatting.parse_date(last["made_up_to"]))
        if last.get("type"):
            add("accounts_type", last["type"].replace("-", " ").capitalize())
        next_due = accounts.get("next_due") or (accounts.get("next_accounts") or {}).get("due_on")
        if next_due:
            add("accounts_next_due", formatting.day(next_due))
        ard = accounts.get("accounting_reference_date") or {}
        if ard.get("day") and ard.get("month"):
            try:
                add("accounting_reference_date", date(2000, int(ard["month"]), int(ard["day"])).strftime("%d %B").lstrip("0"))
            except ValueError:
                pass

        cs = p.get("confirmation_statement") or {}
        if cs.get("last_made_up_to"):
            add("confirmation_statement_last", formatting.day(cs["last_made_up_to"]))
        if cs.get("next_due"):
            add("confirmation_statement_next_due", formatting.day(cs["next_due"]))
        return src

    # -- Officers & PSCs --------------------------------------------------

    def _officers_source(self, number):
        data = self._get(f"/company/{number}/officers", items_per_page=100)
        src = SourceDoc(provider=self.name, external_id=f"officers:{number}", source_type="regulatory_filing",
                        name="Companies House", title="Officers (register)",
                        url=f"{PUBLIC}/company/{number}/officers")
        for o in data.get("items") or []:
            if o.get("resigned_on"):
                continue
            role = (o.get("officer_role") or "officer").replace("-", " ")
            appointed = formatting.day(o.get("appointed_on"))
            text = f"{_title_name(o.get('name'))}: {role}" + (f", appointed {appointed}" if appointed else "")
            src.facts.append(Fact(fact_type="officer", value_text=text, evidence_type="verified",
                                  confidence="high", location="Companies House officers register"))
        return src

    def _psc_source(self, number):
        try:
            data = self._get(f"/company/{number}/persons-with-significant-control", items_per_page=100)
        except NotFound:
            data = {}  # companies with no PSC register entries return 404
        src = SourceDoc(provider=self.name, external_id=f"psc:{number}", source_type="regulatory_filing",
                        name="Companies House", title="Persons with significant control (register)",
                        url=f"{PUBLIC}/company/{number}/persons-with-significant-control")
        for p in data.get("items") or []:
            if p.get("ceased_on") or p.get("ceased"):
                continue
            natures = "; ".join(_nature(n) for n in p.get("natures_of_control") or [])
            text = _title_name(p.get("name")) + (f": {natures}" if natures else "")
            src.facts.append(Fact(fact_type="psc", value_text=text, evidence_type="verified",
                                  confidence="high", location="Companies House PSC register",
                                  as_of=formatting.parse_date(p.get("notified_on")),
                                  notes=(p.get("kind") or "").replace("-", " ") or None))
        return src

    # -- Filing history & documents ---------------------------------------

    def _accounts_filings(self, number):
        data = self._get(f"/company/{number}/filing-history", category="accounts", items_per_page=25)
        items = [i for i in data.get("items") or [] if (i.get("type") or "").startswith("AA")]
        items.sort(key=lambda i: i.get("date") or "", reverse=True)
        return items

    def _accounts_document(self, number, filing):
        tx = filing.get("transaction_id")
        made_up = formatting.parse_date(
            filing.get("action_date") or (filing.get("description_values") or {}).get("made_up_date"))
        filed = formatting.parse_date(filing.get("date"))
        kind = _accounts_kind(filing.get("description"))
        title = f"{kind} made up to {formatting.day(made_up)}" if made_up else kind

        doc = SourceDoc(
            provider=self.name, external_id=f"filing:{tx}", source_type="regulatory_filing",
            name="Companies House", title=title,
            url=f"{PUBLIC}/company/{number}/filing-history/{tx}/document?format=pdf&download=0",
            published_on=filed, period_end=made_up,
            document_type="group_accounts" if "group" in kind.lower() else "annual_accounts",
        )

        meta_url = (filing.get("links") or {}).get("document_metadata")
        if not meta_url:
            doc.parse_status = "no_document"
            doc.notes = "Companies House lists this filing without a downloadable document."
            return doc
        if meta_url.startswith("/"):
            meta_url = DOC_API + meta_url

        try:
            meta = self.http.get_json(meta_url, auth=self.auth)
            formats = set((meta.get("resources") or {}).keys())
            if "application/xhtml+xml" in formats:
                self._parse_ixbrl(doc, meta_url, made_up, kind)
            elif "application/pdf" in formats:
                self._inspect_pdf(doc, meta_url, meta)
            else:
                doc.parse_status = "no_document"
                doc.notes = f"No supported document format (available: {', '.join(formats) or 'none'})."
        except EnrichmentError as exc:
            doc.parse_status = "failed"
            doc.notes = str(exc)
        return doc

    def _download(self, meta_url, content_type):
        # The content endpoint redirects to a short-lived Amazon S3 URL.
        # Http.get passes auth per request, and requests drops it on the
        # cross-host redirect, so the key never reaches S3.
        return self.http.get(meta_url + "/content", auth=self.auth, headers={"Accept": content_type})

    def _parse_ixbrl(self, doc, meta_url, made_up, kind):
        content = self._download(meta_url, "application/xhtml+xml").content
        try:
            values = ixbrl.parse(content, period_end=made_up)
        except Exception as exc:  # malformed markup
            doc.parse_status = "failed"
            doc.notes = f"Could not parse iXBRL document: {type(exc).__name__}"
            return
        doc.parse_status = "parsed"
        doc.notes = (f"Machine-readable (iXBRL) accounts. {len(values)} tagged headline values found."
                     if values else "Machine-readable (iXBRL) accounts, but no headline values are tagged.")
        for v in values:
            doc.period_start = doc.period_start or v.start
            doc.facts.append(_ixbrl_fact(v, kind))

    def _inspect_pdf(self, doc, meta_url, meta):
        """PDF-only filings (often large companies, or older/paper filings).
        We confirm the document is retrievable and whether it has a text
        layer, but don't pull numbers out of free text: that needs a
        document-understanding step (a later, AI-assisted phase)."""
        content = self._download(meta_url, "application/pdf").content
        # Keep the exact bytes so the AI extractor can read them later
        # without re-downloading, and so we can prove what it was shown.
        doc.content_path, doc.content_sha256 = document_cache.save(self.name, content, ".pdf")
        try:
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(content))
            pages = len(reader.pages)
            sample = "".join((reader.pages[i].extract_text() or "") for i in range(min(pages, 3)))
        except Exception as exc:
            doc.parse_status = "failed"
            doc.notes = f"PDF could not be read: {type(exc).__name__}"
            return
        if len(sample.strip()) < 50:
            doc.parse_status = "unreadable"
            doc.notes = (f"PDF ({pages} pages) is a scanned image with no text layer; "
                         "AI extraction can read it, but quotes can't be checked against text.")
        else:
            doc.parse_status = "not_parsed"
            doc.notes = (f"PDF ({pages} pages) with readable text. Figures aren't machine-tagged; "
                         "use AI extraction to read them.")

    # -- Disclosure summary ------------------------------------------------

    def _disclosure_detail(self, latest, filings):
        """Which headline financials the latest accounts don't provide,
        and why, so the UI can say 'No disclosed revenue' rather than
        'Unknown'."""
        if latest is None:
            return {}
        found = {f.fact_type for f in latest.facts}
        missing = [ft for ft in HEADLINE_FINANCIALS if ft not in found]
        if not missing:
            return {}
        if latest.parse_status == "parsed":
            return {"not_disclosed": missing, "not_disclosed_kind": "not_tagged",
                    "not_disclosed_reason": f"Not disclosed in {latest.title}"}
        return {"not_disclosed": missing, "not_disclosed_kind": "pdf_only",
                "not_disclosed_reason": f"{latest.title} is a PDF; figures not extracted yet"}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _ixbrl_fact(v, kind):
    period = formatting.period_label(v.start, v.end)
    if isinstance(v.value, Decimal):
        if v.fact_type == "employee_count":
            text = f"{formatting.count(v.value)} (average)"
        else:
            text = formatting.money(v.value, v.currency)
        number = v.value
    else:
        text, number = v.value, None
    return Fact(
        fact_type=v.fact_type,
        value_text=text,
        value_number=number,
        currency=v.currency,
        period=period,
        period_start=v.start,
        period_end=v.end,
        evidence_type="verified",
        confidence="medium" if v.warning else "high",
        excerpt=v.excerpt,
        location=f"iXBRL tag {v.concept}",
        notes=v.warning or f"From {kind.lower()}",
        extraction_method="xbrl_tag",
    )


def _address(a):
    if not a:
        return None
    parts = [a.get(k) for k in ("premises", "address_line_1", "address_line_2", "locality",
                                "region", "postal_code", "country")]
    return ", ".join(p for p in parts if p)


def _title_name(name):
    """'SMITH, John Andrew' -> 'John Andrew Smith'; company names unchanged."""
    name = name or ""
    if "," in name and name.split(",")[0].isupper():
        surname, rest = name.split(",", 1)
        return f"{rest.strip()} {surname.strip().title()}"
    return name


def _nature(code):
    text = code.replace("-", " ")
    text = re.sub(r"(\d+) to (\d+) percent", r"\1–\2%", text)
    text = text.replace(" percent", "%").replace("more than", ">")
    return text[0].upper() + text[1:] if text else text


def _accounts_kind(description):
    """'accounts-with-accounts-type-full' -> 'Full accounts'."""
    d = (description or "").replace("accounts-with-accounts-type-", "")
    labels = {
        "full": "Full accounts", "group": "Group accounts", "small": "Small company accounts",
        "micro-entity": "Micro-entity accounts", "total-exemption-full": "Total exemption full accounts",
        "total-exemption-small": "Total exemption small accounts", "dormant": "Dormant company accounts",
        "unaudited-abridged": "Unaudited abridged accounts", "audited-abridged": "Audited abridged accounts",
        "medium": "Medium company accounts", "audit-exemption-subsidiary": "Audit-exempt subsidiary accounts",
        "full-group": "Group accounts",
    }
    return labels.get(d) or (d.replace("-", " ").capitalize() if d else "Annual accounts")
