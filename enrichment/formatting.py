"""Display formatting shared by providers, so the same number always reads
the same way (e.g. "£12.4m") whichever source it came from. The exact
number is always kept separately in facts.value_number."""

import re
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal

CURRENCY_SYMBOLS = {"GBP": "£", "USD": "$", "EUR": "€", "JPY": "¥"}


def money(value, currency=None):
    value = Decimal(value)
    sign = "−" if value < 0 else ""
    value = abs(value)
    symbol = CURRENCY_SYMBOLS.get(currency or "", "")
    suffix_code = "" if symbol or not currency else f" {currency}"
    for threshold, unit, places in ((Decimal("1e9"), "bn", 2), (Decimal("1e6"), "m", 2), (Decimal("1e3"), "k", 1)):
        if value >= threshold:
            scaled = (value / threshold).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
            text = f"{scaled:,.{places}f}".rstrip("0").rstrip(".")
            return f"{sign}{symbol}{text}{unit}{suffix_code}"
    return f"{sign}{symbol}{value:,.0f}{suffix_code}"


def count(value):
    return f"{Decimal(value):,.0f}"


def day(value):
    """'2025-03-31' or a date -> '31 Mar 2025'."""
    d = parse_date(value)
    return d.strftime("%d %b %Y").lstrip("0") if d else None


def parse_date(value):
    if value is None or isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def period_label(start, end):
    """A human label for a reporting period."""
    end = parse_date(end)
    start = parse_date(start)
    if end is None:
        return None
    if start is None:
        return f"as at {day(end)}"
    days = (end - start).days
    if 350 <= days <= 380:
        return f"FY ended {day(end)}"
    return f"{day(start)} – {day(end)}"


def normalise_name(name):
    """For exact-name matching across registers: lower case, no punctuation,
    no legal-form suffixes. 'Salesforce, Inc.' -> 'salesforce'."""
    name = re.sub(r"[^a-z0-9 ]+", " ", (name or "").lower())
    words = name.split()
    suffixes = {"limited", "ltd", "plc", "llp", "inc", "incorporated", "corp", "corporation",
                "co", "company", "the", "llc", "sa", "ag", "nv", "se", "gmbh"}
    while words and words[-1] in suffixes:
        words.pop()
    while words and words[0] == "the":
        words.pop(0)
    return " ".join(words)


def squash(text, limit=300):
    text = re.sub(r"\s+", " ", text or "").strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"
