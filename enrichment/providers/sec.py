"""SEC EDGAR: official filings of US-listed companies (and foreign private
issuers filing 20-F).

Endpoints (no key; SEC requires a descriptive User-Agent with contact info):
  https://www.sec.gov/files/company_tickers.json        ticker/name -> CIK
  https://data.sec.gov/submissions/CIK##########.json   identity + filing list
  https://data.sec.gov/api/xbrl/companyfacts/CIK##########.json
      every XBRL-tagged value the company has filed, with the filing
      (accession number) it came from.

Financial facts are the company's own tagged values from its annual
report (10-K / 20-F), so they're stored as verified / high confidence,
each linked to the specific filing it was reported in.
"""

import os
from decimal import Decimal

from .. import formatting
from ..base import (Ambiguous, CredentialsMissing, Fact, NotFound, Provider, ProviderResult,
                    SourceDoc)
from .companies_house import is_uk

ANNUAL_FORMS = ("10-K", "10-K/A", "20-F", "20-F/A", "40-F")
YEARS = 3   # fiscal years of financials to store

# fact_type -> concepts in priority order. Checked per fiscal year, because
# companies switch concepts over time (e.g. Revenues -> RevenueFromContract...).
CONCEPTS = {
    "revenue": ["us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax", "us-gaap:Revenues",
                "us-gaap:SalesRevenueNet", "us-gaap:RevenueFromContractWithCustomerIncludingAssessedTax",
                "ifrs-full:Revenue"],
    "operating_profit": ["us-gaap:OperatingIncomeLoss", "ifrs-full:ProfitLossFromOperatingActivities"],
    "profit_before_tax": [
        "us-gaap:IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
        "us-gaap:IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments",
        "ifrs-full:ProfitLossBeforeTax"],
    "net_income": ["us-gaap:NetIncomeLoss", "ifrs-full:ProfitLoss"],
    "cash": ["us-gaap:CashAndCashEquivalentsAtCarryingValue", "ifrs-full:CashAndCashEquivalents"],
    "total_debt": ["us-gaap:LongTermDebt", "us-gaap:LongTermDebtNoncurrent", "us-gaap:DebtInstrumentCarryingAmount"],
    "total_assets": ["us-gaap:Assets", "ifrs-full:Assets"],
    "total_liabilities": ["us-gaap:Liabilities", "ifrs-full:Liabilities"],
    "net_assets": ["us-gaap:StockholdersEquity", "ifrs-full:Equity"],
}
FLOW_TYPES = {"revenue", "operating_profit", "profit_before_tax", "net_income"}
CONCEPT_NOTES = {
    "us-gaap:LongTermDebt": "Long-term debt including current portion",
    "us-gaap:LongTermDebtNoncurrent": "Long-term debt, non-current portion only",
    "us-gaap:StockholdersEquity": "Total stockholders' equity",
}


class SEC(Provider):
    name = "sec"
    label = "SEC EDGAR"

    def not_applicable_reason(self, ctx):
        if ctx.identifiers.get("sec_cik") or ctx.identifiers.get("ticker"):
            return None
        if is_uk(ctx.company.get("hq_country")):
            return "SEC covers US-listed companies. Add a ticker or CIK if this company files with the SEC."
        return None

    def run(self, ctx):
        agent = os.environ.get("SEC_USER_AGENT")
        if not agent:
            raise CredentialsMissing("SEC requires a contact User-Agent: add SEC_USER_AGENT "
                                     "(e.g. 'Your Name you@example.com') to .env.")
        self.http = ctx.http
        self.headers = {"User-Agent": agent}
        result = ProviderResult()

        cik = ctx.identifiers.get("sec_cik")
        if cik:
            cik = int(cik)
        else:
            cik, ticker, method = self._match(ctx.company["name"], ctx.identifiers.get("ticker"))
            result.identifiers["sec_cik"] = (str(cik), method)

        subs = self.http.get_json(f"https://data.sec.gov/submissions/CIK{cik:010d}.json", headers=self.headers)
        if subs.get("tickers") and not ctx.identifiers.get("ticker"):
            result.identifiers["ticker"] = (subs["tickers"][0], "derived")
        if subs.get("website"):
            result.website = subs["website"]

        result.sources.append(self._profile_source(cik, subs))
        filings = self._annual_filings(cik, subs)

        try:
            facts = self.http.get_json(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json",
                                       headers=self.headers)
            self._attach_financials(filings, facts)
        except NotFound:
            result.status = "partial"
            result.message = "No XBRL financial data published for this company."

        result.sources.extend(filings.values())
        if not result.message:
            n = sum(len(s.facts) for s in result.sources)
            result.message = f"{subs.get('name')} (CIK {cik}): {n} facts, {len(filings)} annual filings."
        return result

    # -- Matching ---------------------------------------------------------

    def _match(self, name, ticker):
        data = self.http.get_json("https://www.sec.gov/files/company_tickers.json", headers=self.headers)
        rows = list(data.values()) if isinstance(data, dict) else []
        if ticker:
            hits = [r for r in rows if r.get("ticker", "").upper() == ticker.upper().split(".")[0]]
            if hits:
                return int(hits[0]["cik_str"]), hits[0]["ticker"], "derived"
        target = formatting.normalise_name(name)
        hits = {r["cik_str"]: r for r in rows if formatting.normalise_name(r.get("title")) == target}
        if len(hits) == 1:
            r = next(iter(hits.values()))
            return int(r["cik_str"]), r["ticker"], "auto_exact_name"
        if not hits:
            raise NotFound(f"No SEC-registered company named exactly '{name}'. If it is US-listed, add its ticker or CIK.")
        raise Ambiguous(f"Several SEC registrants are named '{name}'. Add the ticker or CIK.",
                        {"candidates": [{"name": r["title"], "number": r["cik_str"], "status": r["ticker"]}
                                        for r in list(hits.values())[:5]]})

    # -- Identity ---------------------------------------------------------

    def _profile_source(self, cik, s):
        src = SourceDoc(provider=self.name, external_id=f"submissions:{cik}", source_type="regulatory_filing",
                        name="SEC EDGAR", title="Company filings index",
                        url=f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}")

        def add(fact_type, value, **kw):
            if value:
                src.facts.append(Fact(fact_type=fact_type, value_text=str(value), evidence_type="verified",
                                      confidence="high", location="SEC EDGAR submissions record", **kw))

        add("legal_name", s.get("name"))
        tickers, exchanges = s.get("tickers") or [], s.get("exchanges") or []
        if tickers:
            add("stock_ticker", ", ".join(tickers))
        if exchanges:
            add("exchange", ", ".join(e for e in exchanges if e))
        if tickers and exchanges:
            add("ownership_status", f"Publicly listed ({', '.join(e for e in exchanges if e)})",
                notes="Listed securities registered with the SEC")
        if s.get("sic"):
            add("sic_code", f"{s['sic']} (US SIC): {s.get('sicDescription') or ''}".strip(": "))
        biz = (s.get("addresses") or {}).get("business") or {}
        hq = ", ".join(p for p in (biz.get("city", "").title(), biz.get("stateOrCountryDescription")) if p)
        add("headquarters", hq, notes="Business address filed with the SEC")
        return src

    # -- Filings & financials ----------------------------------------------

    def _annual_filings(self, cik, subs):
        """{accession: SourceDoc} for recent annual reports."""
        recent = (subs.get("filings") or {}).get("recent") or {}
        filings = {}
        for i, form in enumerate(recent.get("form") or []):
            if form not in ANNUAL_FORMS:
                continue
            acc = recent["accessionNumber"][i]
            doc = recent["primaryDocument"][i]
            filings[acc] = SourceDoc(
                provider=self.name, external_id=f"filing:{acc}", source_type="regulatory_filing",
                name="SEC EDGAR", title=f"Form {form} for period ending {formatting.day(recent['reportDate'][i])}",
                url=f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc.replace('-', '')}/{doc}",
                published_on=formatting.parse_date(recent["filingDate"][i]),
                period_end=formatting.parse_date(recent["reportDate"][i]),
                document_type="annual_report_" + form.split("/")[0].lower().replace("-", ""),
                parse_status="parsed",
                notes="Financial values read from the XBRL data tagged in this filing.",
            )
            if len(filings) >= YEARS + 2:
                break
        return filings

    def _attach_financials(self, filings, companyfacts):
        all_facts = companyfacts.get("facts") or {}

        for fact_type, concepts in CONCEPTS.items():
            chosen = {}   # period end -> (concept, entry)
            for concept in concepts:
                taxonomy, local = concept.split(":")
                units = ((all_facts.get(taxonomy) or {}).get(local) or {}).get("units") or {}
                currency = next((u for u in units if len(u) == 3 and u.isupper()), None)
                if not currency:
                    continue
                for e in units[currency]:
                    if e.get("form") not in ANNUAL_FORMS or not _is_annual(e, fact_type):
                        continue
                    end = e["end"]
                    # Earliest filing that reported this period = the year's own annual report.
                    if end in chosen and chosen[end][0] != concept:
                        continue  # a higher-priority concept already covers this year
                    if end not in chosen or e["filed"] < chosen[end][1]["filed"]:
                        chosen[end] = (concept, e, currency)
            for end in sorted(chosen, reverse=True)[:YEARS]:
                concept, e, currency = chosen[end]
                doc = filings.get(e["accn"])
                if doc is None:
                    continue   # value from a filing older than the ones we list
                value = Decimal(str(e["val"]))
                start = formatting.parse_date(e.get("start"))
                doc.facts.append(Fact(
                    fact_type=fact_type,
                    value_text=formatting.money(value, currency),
                    value_number=value,
                    currency=currency,
                    period=formatting.period_label(start, end),
                    period_start=start,
                    period_end=formatting.parse_date(end),
                    evidence_type="verified",
                    confidence="high",
                    location=f"XBRL {concept} in Form {e['form']} filed {formatting.day(e['filed'])}",
                    notes=CONCEPT_NOTES.get(concept),
                    extraction_method="xbrl_tag",
                ))


def _is_annual(entry, fact_type):
    if fact_type not in FLOW_TYPES:
        return "start" not in entry     # balance-sheet value at a point in time
    start, end = formatting.parse_date(entry.get("start")), formatting.parse_date(entry.get("end"))
    return bool(start and end) and 350 <= (end - start).days <= 380
