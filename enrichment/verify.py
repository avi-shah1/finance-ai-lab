"""Deterministic checks on what the model returned: is the quoted evidence
really in the source, and is the number really in the quote?

These run on every AI-extracted fact before it is stored. A fact that
fails is rejected (and logged), never stored.
"""

import re
from decimal import Decimal

_TRANSLATE = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"', "–": "-", "—": "-",
                            "−": "-", " ": " ", "£": "£"})


def _squeeze(text):
    """Lower-case, normalise punctuation, and drop all whitespace: PDF text
    extraction often loses or adds spaces and line breaks."""
    return re.sub(r"\s+", "", (text or "").translate(_TRANSLATE).lower())


def quote_in_text(quote, text):
    """True if the quote appears in the text, allowing for whitespace and
    punctuation differences, or if every token of the quote appears (PDF
    tables are sometimes extracted in a different cell order)."""
    q, t = _squeeze(quote), _squeeze(text)
    if not q or not t:
        return False
    if q in t:
        return True
    tokens = [tok for tok in re.findall(r"[a-z0-9][a-z0-9.,'()%-]*", (quote or "").translate(_TRANSLATE).lower())
              if len(tok) >= 2]
    return len(tokens) >= 2 and all(_squeeze(tok) in t for tok in tokens)


def number_in_quote(value, quote):
    """True if the figure (as printed, before any unit multiplier) appears in
    the quote. Blocks derived or calculated numbers: those won't be printed."""
    number = abs(Decimal(str(value)))
    candidates = {format(number.normalize(), "f")}
    if number == number.to_integral_value():
        candidates.add(str(int(number)))
    digits_in_quote = re.sub(r"[,\s]", "", quote or "")
    return any(c in digits_in_quote for c in candidates)
