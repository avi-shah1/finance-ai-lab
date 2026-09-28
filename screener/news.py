"""Recent developments for one company: one Exa news search, then Claude
(Haiku) lists the dated events the articles report.

Checks before anything is stored:
  * the article must mention the company (name or domain), in code;
  * Claude must say the event is about this company, not a namesake;
  * the quoted evidence must really be in the article (verify.quote_in_text).
Events are stored as third-party evidence (news), or as company-reported
when the article is on the company's own website (a press release).
"""

import re
from datetime import date

from discovery.sources.exa import ExaSource
from domains import normalise_domain
from enrichment import formatting, llm, store, verify
from enrichment.base import CredentialsMissing, SourceDoc

from .schemas import NewsExtraction

PROVIDER = "news"
NUM_RESULTS = 6
TEXT_CHARS = 2500

SYSTEM = """You read news articles for an M&A research tool and list the dated developments they report about one named company.

Rules:
- Only events about the named company itself. If an article is about a different company with a similar name, set about_this_company to false.
- Only what the article states. No inference, no outside knowledge.
- Event types: acquisition (it bought or was bought), fundraising, product_launch, partnership, geographic_expansion, hiring (a stated hiring push or headcount change), leadership_change, financial_results, regulatory, other.
- evidence_text must be copied character-for-character from the article.
- One event per development; if several articles report the same development, list it once.
- Returning an empty list is fine."""


def gather_news(company, search_id=None, source=None):
    """Search, extract and store news events for a company.
    Returns (status, message, events_stored)."""
    source = source or ExaSource()
    if not source.available():
        raise CredentialsMissing("API credentials required: add EXA_API_KEY to .env.")
    name = company["name"]
    query = (f"{name} ({company.get('domain') or ''}) news: funding, acquisition, partnership, "
             "product launch, expansion, leadership")
    hits = source.search(query, category="news", num_results=NUM_RESULTS, search_id=search_id,
                         task="screening_news_search", text_chars=TEXT_CHARS, company_id=company["id"])
    articles = [h for h in hits if mentions(company, h)]
    if not articles:
        store.save_events(company["id"], PROVIDER, [])
        return "not_found", f"No news articles mentioning {name} found.", 0

    ids = {f"A{i}": h for i, h in enumerate(articles, start=1)}
    blocks = [
        f'<article id="{aid}" url="{h.url}" published="{(h.published or "")[:10]}">\n'
        f"Title: {h.title or ''}\n{(h.text or '').strip()}\n</article>"
        for aid, h in ids.items()
    ]
    content = [{"type": "text", "text": (
        f"Company: {name} (website: {company.get('domain') or 'unknown'})\n\n" + "\n\n".join(blocks))}]
    parsed, meta = llm.extract(SYSTEM, content, NewsExtraction, task="screening_news", max_tokens=4000,
                               search_id=search_id, company_id=company["id"])

    by_article, rejected = {}, 0
    for ev in parsed.events:
        hit = ids.get(ev.article_id)
        if hit is None or not ev.about_this_company or \
                not verify.quote_in_text(ev.evidence_text, f"{hit.title or ''}\n{hit.text or ''}"):
            rejected += 1
            continue
        by_article.setdefault(ev.article_id, []).append({
            "event_type": ev.event_type,
            "title": formatting.squash(ev.title, 150),
            "summary": formatting.squash(ev.summary, 400),
            "event_date": _event_date(ev.event_date) or formatting.parse_date(hit.published),
            "excerpt": formatting.squash(ev.evidence_text, 300),
            "evidence_type": "company_reported" if _own_site(company, hit) else "third_party",
        })

    items = [(_article_source(company, ids[aid]), events) for aid, events in by_article.items()]
    stored = store.save_events(company["id"], PROVIDER, items)
    message = f"{stored} events from {len(articles)} articles" + (f"; {rejected} rejected by checks" if rejected else "")
    return "success", message + ".", stored


def mentions(company, hit):
    """Does the article name the company (ignoring Ltd/GmbH etc.) or its domain?"""
    text = f"{hit.title or ''} {hit.text or ''}".lower()
    domain = company.get("domain")
    if domain and domain in text:
        return True
    # "HealthOne (Cegedim Group)" -> "healthone"; "Crossuite bv" -> "crossuite"
    key = formatting.normalise_name(re.sub(r"\(.*?\)", " ", company["name"]))
    return bool(key) and re.search(rf"\b{re.escape(key)}\b", formatting.normalise_name(text)) is not None


def _own_site(company, hit):
    return bool(company.get("domain")) and normalise_domain(hit.url) == company["domain"]


def _article_source(company, hit):
    own = _own_site(company, hit)
    return SourceDoc(provider=PROVIDER, external_id=hit.url,
                     source_type="press_release" if own else "news_article",
                     name=normalise_domain(hit.url) or "News", url=hit.url,
                     title=formatting.squash(hit.title, 200) if hit.title else None,
                     published_on=formatting.parse_date(hit.published))


def _event_date(value):
    """'2025-03-14' -> date; '2025-03' -> 1 March 2025; anything else -> None."""
    if not value:
        return None
    if re.fullmatch(r"\d{4}-\d{2}", value):
        value += "-01"
    parsed = formatting.parse_date(value)
    return parsed if parsed and parsed <= date.today() else None
