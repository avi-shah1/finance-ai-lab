"""Read the size fields from Exa's company profiles. Plain Python, no AI.

For the "company" category Exa returns its own profile text rather than the
company's homepage, in a consistent form, e.g.:

    Crossuite bv is a Hospitals and Health Care company. ... Crossuite bv
    employs 18 people (+63.6% YoY, +7 people) and has an annual revenue of
    $5.5M, founded in 2007. Headquartered in Antwerpen, Antwerpen, Belgium,
    with presence in Netherlands.

These are a data vendor's figures (the revenue is an estimate), so they are
stored as third-party evidence, never as the company's own statement.
"""

import re
from dataclasses import dataclass
from decimal import Decimal

EMPLOYEES = re.compile(r"\bemploys ([\d,]+) (?:people|employees)(?: \(([^)]*)\))?")
REVENUE = re.compile(r"annual revenue of (\$|€|£|USD|EUR|GBP)\s?([\d.,]+)\s?([KMB])?\b")
FOUNDED = re.compile(r"\bfounded in (1[89]\d\d|20\d\d)\b")
# Stops before trailing clauses: ", with presence in ...", ", with an office in ...", ", operates in ..."
HEADQUARTERS = re.compile(r"\bHeadquartered in ([^.\n]+?)(?:,? (?:with|operates) [^.\n]*)?\.")
CURRENCIES = {"$": "USD", "€": "EUR", "£": "GBP", "USD": "USD", "EUR": "EUR", "GBP": "GBP"}
MULTIPLIERS = {None: 1, "K": 1_000, "M": 1_000_000, "B": 1_000_000_000}


@dataclass
class Profile:
    employees: int | None = None
    employee_growth: str | None = None     # e.g. "+63.6% YoY, +7 people"
    employees_quote: str | None = None
    revenue: Decimal | None = None
    revenue_currency: str | None = None
    revenue_quote: str | None = None
    founded: int | None = None
    headquarters: str | None = None
    headquarters_quote: str | None = None


def parse_profile(text):
    p = Profile()
    text = text or ""
    if m := EMPLOYEES.search(text):
        p.employees = int(m.group(1).replace(",", ""))
        p.employee_growth = m.group(2)
        p.employees_quote = m.group(0)
    if m := REVENUE.search(text):
        try:
            amount = Decimal(m.group(2).replace(",", ""))
        except ArithmeticError:
            amount = None
        if amount is not None:
            p.revenue = amount * MULTIPLIERS[m.group(3)]
            p.revenue_currency = CURRENCIES[m.group(1)]
            p.revenue_quote = m.group(0)
    if m := FOUNDED.search(text):
        p.founded = int(m.group(1))
    if m := HEADQUARTERS.search(text):
        p.headquarters = m.group(1).strip()
        p.headquarters_quote = m.group(0)
    return p
