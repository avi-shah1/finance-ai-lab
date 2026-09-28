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
                "co", "company", "the", "llc", "sa", "ag", "nv", "se", "gmbh",
                # other European legal forms, e.g. "Crossuite bv", "CompuGroup Medical SE & Co. KGaA"
                "bv", "bvba", "sas", "sarl", "srl", "spa", "ab", "as", "asa", "oy", "oyj", "aps",
                "kg", "kgaa", "ug", "sl", "slu", "sro", "pty", "pte"}
    while words and words[-1] in suffixes:
        words.pop()
    while words and words[0] == "the":
        words.pop(0)
    return " ".join(words)


SUMMARY_BULLET_RULES = (
    "2-3 short bullet points on what the company does: what it sells, to whom, and where. "
    "Third person and neutral (e.g. 'Sells cloud practice-management software to UK dental clinics'), "
    "never 'you', 'we' or marketing language. At most 15 words each. Only facts stated in the text.")


def clean_bullets(bullets, limit=3):
    """Tidy Claude's summary bullets: drop list markers and blanks, cap the
    length and count. Returns the bullets joined by newlines (or None)."""
    cleaned = []
    for b in bullets or []:
        b = re.sub(r"^[\s•\-–*·]+", "", b or "").strip()
        if b and b.lower() not in {c.lower() for c in cleaned}:
            cleaned.append(squash(b, 140))
    return "\n".join(cleaned[:limit]) or None


# Evidence IDs Claude cites (F988 = fact 988, E13 = event 13, BRIEF = the
# buyer brief). They are stored separately and shown as source chips, so
# strip_refs() removes any Claude also wrote into its prose.
_REF = r"(?:[FE]\d{1,7}(?:\s*[-–]\s*[FE]?\d{1,7})?|BRIEF)"
_REF_GROUP = re.compile(rf"\s*[(\[]\s*{_REF}(?:\s*(?:,|;|and|&)\s*{_REF})*\s*[)\]]")
_REF_BARE = re.compile(rf"(?:\s*(?:,|;|and|&)\s*)?\b(?:per|see|cf\.?)?\s*\b{_REF}\b")


def strip_refs(text):
    """'...in the UK (F988, F1036). Salesforce (F751) is...' -> '...in the UK. Salesforce is...'"""
    if not text:
        return text
    text = _REF_GROUP.sub("", text)
    text = _REF_BARE.sub("", text)
    text = re.sub(r"\(\s*\)", "", text)                  # emptied brackets
    text = re.sub(r"\s+([,.;:!?)])", r"\1", text)       # space before punctuation
    text = re.sub(r"([,;])\s*([.;])", r"\2", text)     # ", ." left behind
    return re.sub(r"\s{2,}", " ", text).strip()


def gist(text, limit=120):
    """A short version of a longer text that ends cleanly: the first
    sentence, and if that is still too long, cut at the last comma, semicolon
    or dash before the limit (at a word boundary as a last resort, never
    mid-word)."""
    text = re.sub(r"\s+", " ", text or "").strip()
    first = re.split(r"(?<=[.!?])\s+(?=[A-Z])", text, maxsplit=1)[0]
    if len(first) <= limit:
        return first
    head = first[:limit]
    cut = max(head.rfind(", "), head.rfind("; "), head.rfind(" – "), head.rfind(" - "))
    if cut >= limit // 2:
        return head[:cut].rstrip(" ,;–-") + "."
    return head[:head.rfind(" ")].rstrip(" ,;–-") + "…"


def squash(text, limit=300):
    text = re.sub(r"\s+", " ", text or "").strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"
