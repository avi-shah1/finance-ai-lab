"""Deterministic extraction of financial facts from iXBRL accounts.

Most UK accounts filed at Companies House since ~2011 are "inline XBRL":
an ordinary XHTML page where each number is wrapped in a tag that says
exactly what it is, for which period, in which currency. For example:

    <ix:nonFraction name="uk-core:TurnoverRevenue" contextRef="FY2025"
                    unitRef="GBP" decimals="0" scale="0">12,400,000</ix:nonFraction>

    <xbrli:context id="FY2025">
        <xbrli:period><xbrli:startDate>2024-04-01</xbrli:startDate>
                      <xbrli:endDate>2025-03-31</xbrli:endDate></xbrli:period>
    </xbrli:context>

Because the company itself tagged the value, reading it involves no
guesswork, so these facts are stored as `verified` / `high` confidence.
If a concept isn't tagged, we return nothing for it: never an estimate.
"""

import warnings
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation

from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning

from . import formatting

# fact_type -> XBRL concept local names, in priority order. Names cover the
# FRC UK taxonomies (uk-core / older uk-gaap) and IFRS (ifrs-full).
NUMERIC_CONCEPTS = {
    "revenue": ["TurnoverRevenue", "Turnover", "Revenue", "TurnoverGrossOperatingRevenue"],
    "gross_profit": ["GrossProfitLoss", "GrossProfit"],
    "operating_profit": ["OperatingProfitLoss", "ProfitLossFromOperatingActivities"],
    "profit_before_tax": ["ProfitLossOnOrdinaryActivitiesBeforeTax", "ProfitLossBeforeTax"],
    "net_income": ["ProfitLoss", "ProfitLossForPeriod"],
    "cash": ["CashBankOnHand", "CashBankInHand", "CashCashEquivalents", "CashAndCashEquivalents"],
    "total_assets": ["Assets"],
    "total_liabilities": ["Liabilities"],
    "total_assets_less_current_liabilities": ["TotalAssetsLessCurrentLiabilities"],
    "net_assets": ["NetAssetsLiabilities", "NetAssetsLiabilitiesIncludingPensionAssetLiability", "Equity"],
    "employee_count": ["AverageNumberEmployeesDuringPeriod", "AverageNumberOfEmployees"],
}

TEXT_CONCEPTS = {
    "auditor": ["NameEntityAuditors", "NameIndividualAuditor"],
}

# Flows (profit & loss) cover a period; balances are at a point in time.
FLOW_TYPES = {"revenue", "gross_profit", "operating_profit", "profit_before_tax", "net_income",
              "employee_count"}


@dataclass
class _Context:
    start: date | None
    end: date | None          # endDate, or instant
    dimensional: bool         # has segment/scenario qualifiers (e.g. "within one year")


@dataclass
class ExtractedValue:
    fact_type: str
    concept: str              # full tag name, e.g. "uk-core:TurnoverRevenue"
    value: Decimal | str
    currency: str | None
    start: date | None
    end: date
    excerpt: str | None       # text of the table row containing the value
    warning: str | None = None  # set when the document looks inconsistent


def parse(document, period_end=None):
    """Return ExtractedValues for the reporting period ending `period_end`
    (the accounts' made-up-to date). If not given, the latest period in the
    document is used. Only undimensioned values are taken, i.e. the headline
    figure rather than a breakdown."""
    # lxml's HTML parser copes with the many slightly-broken XHTML files
    # produced by accounts software; the XML-vs-HTML warning is expected.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", XMLParsedAsHTMLWarning)
        soup = BeautifulSoup(document, "lxml")
    contexts = _read_contexts(soup)
    units = _read_units(soup)
    if not contexts:
        return []

    if period_end is None:
        period_end = max(c.end for c in contexts.values() if c.end)

    found = {}
    for tag in soup.find_all(["ix:nonfraction", "nonfraction"]):
        _collect(found, tag, NUMERIC_CONCEPTS, contexts, period_end, units, numeric=True)
    for tag in soup.find_all(["ix:nonnumeric", "nonnumeric"]):
        _collect(found, tag, TEXT_CONCEPTS, contexts, period_end, units, numeric=False)

    # Keep the highest-priority concept for each fact type.
    results = []
    for fact_type, candidates in found.items():
        concepts = {**NUMERIC_CONCEPTS, **TEXT_CONCEPTS}[fact_type]
        candidates.sort(key=lambda v: concepts.index(v.concept.split(":")[-1]))
        results.append(candidates[0])
    return results


def _collect(found, tag, concept_map, contexts, period_end, units, numeric):
    name = tag.get("name", "")
    local = name.split(":")[-1]
    fact_type = next((ft for ft, names in concept_map.items() if local in names), None)
    if fact_type is None:
        return
    ctx = contexts.get(tag.get("contextref"))
    if ctx is None or ctx.dimensional or ctx.end != period_end:
        return
    if fact_type in FLOW_TYPES and ctx.start is None:
        return
    hidden = tag.find_parent(["ix:hidden", "hidden"]) is not None
    existing = next((v for v in found.get(fact_type, []) if v.concept == name), None)
    if existing and (existing.excerpt or hidden):
        return  # same value repeated elsewhere; keep the visible occurrence

    if numeric:
        value = _number(tag)
        if value is None:
            return
    else:
        value = formatting.squash(tag.get_text(" "), 200)
        if not value:
            return

    warning = None
    if numeric and value > 0 and _displayed_in_brackets(tag):
        warning = ("Shown in brackets in the document (usually negative) but tagged as "
                   "positive. Check the filed document.")

    extracted = ExtractedValue(
        fact_type=fact_type,
        concept=name,
        value=value,
        # Only money has a currency; some filers wrongly give counts a GBP unit.
        currency=units.get(tag.get("unitref")) if fact_type != "employee_count" else None,
        start=ctx.start,
        end=ctx.end,
        excerpt=None if hidden else _row_text(tag),
        warning=warning,
    )
    if existing:
        found[fact_type].remove(existing)
    found.setdefault(fact_type, []).append(extracted)


def _number(tag):
    text = tag.get_text().strip()
    fmt = (tag.get("format") or "").lower()
    if "zerodash" in fmt or "fixed-zero" in fmt or text in {"-", "–", "—", "nil", ""}:
        value = Decimal(0)
    else:
        if "commadecimal" in fmt.replace("-", ""):
            # European style, e.g. ixt:numcommadecimal "1.234,56"
            text = text.replace(".", "").replace(" ", "").replace(",", ".")
        else:
            text = text.replace(",", "").replace(" ", "")
        text = text.strip("()")
        try:
            value = Decimal(text)
        except InvalidOperation:
            return None
    scale = tag.get("scale")
    if scale:
        try:
            value *= Decimal(10) ** int(scale)
        except ValueError:
            return None
    if tag.get("sign") == "-":
        value = -value
    return value


def _displayed_in_brackets(tag):
    def neighbour_text(node, step):
        node = getattr(node, step)
        while node is not None:
            text = node.get_text() if hasattr(node, "get_text") else str(node)
            if text.strip():
                return text.strip()
            node = getattr(node, step)
        return ""
    return (neighbour_text(tag, "previous_sibling").endswith("(")
            and neighbour_text(tag, "next_sibling").startswith(")"))


def _read_contexts(soup):
    contexts = {}
    for ctx in soup.find_all(["xbrli:context", "context"]):
        cid = ctx.get("id")
        if not cid:
            continue
        instant = _date(ctx.find(["xbrli:instant", "instant"]))
        start = _date(ctx.find(["xbrli:startdate", "startdate"]))
        end = _date(ctx.find(["xbrli:enddate", "enddate"])) or instant
        dimensional = ctx.find(["xbrli:segment", "segment", "xbrli:scenario", "scenario"]) is not None
        contexts[cid] = _Context(start=start if not instant else None, end=end, dimensional=dimensional)
    return contexts


def _read_units(soup):
    units = {}
    for unit in soup.find_all(["xbrli:unit", "unit"]):
        measure = unit.find(["xbrli:measure", "measure"])
        if unit.get("id") and measure:
            text = measure.get_text().strip()
            if text.lower().startswith("iso4217:"):
                units[unit["id"]] = text.split(":")[1].upper()
    return units


def _date(tag):
    return formatting.parse_date(tag.get_text().strip()) if tag else None


def _row_text(tag):
    """The visible text around a value, usually its table row, e.g.
    'Turnover 3 12,400,000 11,020,000'. This is the supporting excerpt."""
    row = tag.find_parent(["tr"]) or tag.find_parent(["p", "div"]) or tag
    return formatting.squash(row.get_text(" "), 240)
