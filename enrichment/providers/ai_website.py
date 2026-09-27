"""AI extraction of factual company information from website pages that the
website provider has already retrieved (their visible text is cached).

The model reads the pages and lists only what they explicitly state:
products, capabilities, industries served, customer segments, named
customers, integrations and geographies. Every item must name the page URL
it came from and quote that page; both are checked before storing.
Facts are attached to the page's existing source row, stored as
evidence_type 'ai_extracted', extraction_method 'llm_website'.
"""

from .. import document_cache, formatting, llm, verify
from ..ai_schemas import WebsiteExtraction
from ..base import EnrichmentError, Fact, Provider, ProviderResult

MAX_PAGES = 10
MAX_CHARS_PER_PAGE = 12000

SYSTEM = """You extract factual information about a company from pages of its own website, for an M&A research tool.

Rules:
- List only things the pages explicitly state. Do not infer, generalise or guess.
- Categories:
  product: a named product or product line the company sells.
  capability: a named solution area or capability it says it offers.
  industry_served: an industry or sector it explicitly says it serves.
  customer_segment: a type of customer it explicitly says it serves (e.g. 'NHS trusts', 'mid-market retailers').
  notable_customer: a named customer organisation (only if the page presents it as a customer).
  integration: a named third-party product it integrates with.
  geography_presence: a country or region where it explicitly says it operates, has offices or serves customers.
- Ignore navigation boilerplate, cookie banners, job adverts, and marketing slogans that are not facts.
- source_url must be exactly one of the page URLs given. evidence_text must be copied character-for-character from that page.
- Keep value short (a name, not a sentence). If unsure whether something qualifies, leave it out.
- Returning an empty list is acceptable."""


class AIWebsite(Provider):
    name = "ai_website"
    label = "AI: company website"

    def _pages(self, ctx):
        return [s for s in ctx.sources
                if s.get("provider") == "website" and s.get("source_type") == "company_website"
                and s.get("content_path")][:MAX_PAGES]

    def not_applicable_reason(self, ctx):
        if not self._pages(ctx):
            return "No website pages retrieved yet. Add a website and run research first."
        return None

    def run(self, ctx):
        llm.require_key()
        pages = self._pages(ctx)
        if all(p.get("ai_extracted_sha256") and p["ai_extracted_sha256"] == p.get("content_sha256") for p in pages):
            return ProviderResult(message=f"All {len(pages)} pages already extracted (unchanged since last run).")

        texts = {}
        blocks = []
        for p in pages:
            raw = document_cache.load(p["content_path"])
            if raw is None:
                continue
            text = raw.decode("utf-8", errors="replace")
            texts[p["url"]] = text
            clipped = text[:MAX_CHARS_PER_PAGE]
            blocks.append(f'<page url="{p["url"]}">\n{clipped}\n</page>')
        if not blocks:
            raise EnrichmentError("Cached page text is missing. Re-run research to retrieve the pages.")

        content = [{"type": "text", "text": (
            f"Company: {ctx.company['name']}\n\nWebsite pages:\n\n" + "\n\n".join(blocks)
            + "\n\nExtract the facts described by the schema.")}]
        parsed, meta = llm.extract(SYSTEM, content, WebsiteExtraction)

        by_url = {p["url"]: p for p in pages}
        facts_by_source = {p["id"]: [] for p in pages if p["url"] in texts}
        rejected, seen = [], set()
        for item in parsed.items:
            value = formatting.squash(item.value, 80)
            key = (item.fact_type, value.lower())
            page = by_url.get(item.source_url)
            reason = None
            if page is None or item.source_url not in texts:
                reason = "source_url is not one of the pages provided"
            elif not value:
                reason = "empty value"
            elif not verify.quote_in_text(item.evidence_text, texts[item.source_url]):
                reason = "quote not found on the page"
            elif key in seen:
                continue
            if reason:
                rejected.append({"fact_type": item.fact_type, "value": value, "reason": reason})
                continue
            seen.add(key)
            facts_by_source[page["id"]].append(Fact(
                fact_type=item.fact_type, value_text=value, evidence_type="ai_extracted",
                confidence=item.confidence, excerpt=formatting.squash(item.evidence_text, 300),
                location=item.source_url, extraction_method="llm_website",
                extraction_model=meta["model"], verification="quote_verified",
            ))

        result = ProviderResult()
        for page in pages:
            if page["id"] in facts_by_source:
                result.attach[page["id"]] = {"facts": facts_by_source[page["id"]],
                                             "content_sha256": page.get("content_sha256")}
        stored = sum(len(f) for f in facts_by_source.values())
        result.message = (f"{stored} facts from {len(blocks)} pages"
                          + (f"; {len(rejected)} rejected by verification" if rejected else "") + ".")
        result.detail = {"rejected": rejected, "model": meta["model"], "input_tokens": meta["input_tokens"],
                         "output_tokens": meta["output_tokens"], "request_id": meta["request_id"]}
        return result
