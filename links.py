"""Where an evidence link should take the reader.

A link to a web page on its own lands at the top of the page, which is
little use when the evidence is one sentence halfway down. Browsers that
support "text fragments" (Chrome, Edge, Safari, recent Firefox) scroll to
and highlight text named after "#:~:text=", so evidence links carry the
quote we stored. Quotes from AI extraction were checked word-for-word
against the page when stored, so they match; if the page has changed since,
the browser simply opens it at the top, as before.

  * PDFs open at the cited page (#page=N) instead.
  * Facts read from Exa's company profile are not on the company's website
    at all, so they get no link rather than a misleading one.
"""

import re
from urllib.parse import quote

# The "fact" is a link label harvested from the site, e.g. 'Link "Patient App" -> https://...'.
_LINK_EXCERPT = re.compile(r'^Link "(.+?)" →')


def external_url(value):
    """A safe http(s) link for a stored URL/website, or None. Bare domains
    like 'acme.com' become https://acme.com; other schemes are refused."""
    if not value:
        return None
    value = value.strip()
    if re.match(r"^https?://", value, re.I):
        return value
    if re.match(r"^[\w.-]+\.[a-z]{2,}(/\S*)?$", value, re.I):
        return "https://" + value
    return None


def is_off_site(item):
    """Evidence read from Exa's company profile: a data provider's text,
    not something the linked website says."""
    return item.get("extracted_by") == "discovery" or (
        item.get("provider") == "discovery" and item.get("source_type") == "third_party_database")


def evidence_href(item):
    """The best link for a fact or event row (with its joined source
    columns), or '' when there is no page that shows it."""
    base = external_url(item.get("source_url"))
    if not base or is_off_site(item):
        return ""
    base = base.split("#", 1)[0]
    if item.get("evidence_page"):
        return f"{base}#page={item['evidence_page']}"
    text = _quote_for(item)
    return f"{base}#:~:text={text_fragment(text)}" if text else base


def _quote_for(item):
    excerpt = (item.get("excerpt") or "").strip()
    link = _LINK_EXCERPT.match(excerpt)
    if link:
        return link.group(1)              # the link label is what appears on the page
    if item.get("extraction_method") in ("llm_document", "llm_document_text", "xbrl_tag", "structured_api",
                                         "third_party_api"):
        return ""                         # figures from filings / APIs: no quotable page text
    return excerpt.rstrip("…").strip()


def text_fragment(text, edge_words=5):
    """Encode text for #:~:text=. Long quotes use 'start,end' (first and last
    few words), which matches even when the middle wraps across elements."""
    words = re.sub(r"\s+", " ", text).strip().split(" ")
    if len(words) <= edge_words * 2:
        return _encode(" ".join(words))
    return _encode(" ".join(words[:edge_words])) + "," + _encode(" ".join(words[-edge_words:]))


def _encode(text):
    # '-', ',' and '&' have special meaning inside a text fragment.
    return quote(text, safe="").replace("-", "%2D")
