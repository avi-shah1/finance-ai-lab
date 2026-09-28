"""AI extraction of financial facts from filed PDF accounts that couldn't be
read deterministically (no iXBRL tags: typically large companies' full
accounts or scanned paper filings).

Input:  PDFs already retrieved by the Companies House provider and cached
        (sources.content_path). Nothing new is fetched.
Output: facts attached to the ORIGINAL filing source (so the Companies House
        document link is preserved), stored as evidence_type 'ai_extracted',
        extraction_method 'llm_document', with the page they came from.

Every figure must pass two checks before it is stored:
  1. the number is printed in the quoted evidence (no derived figures);
  2. the quote is found on the cited page's text, when the page has a text
     layer. Scanned pages can't be checked: those facts are marked
     'not_verifiable' and capped at medium confidence.
"""

import base64
import io
import re
from decimal import Decimal

from .. import document_cache, formatting, llm, verify
from ..ai_schemas import AccountsExtraction
from ..base import (CredentialsMissing, EnrichmentError, Fact, Provider, ProviderResult,
                    RateLimited)

DOCUMENT_TYPES = {"annual_accounts", "group_accounts", "annual_report"}
MAX_DOCUMENTS = 2          # most recent unparsed filings per run (cost control)
FULL_DOCUMENT_PAGES = 60   # send whole PDFs up to this length
SUBSET_MAX_PAGES = 40
MAX_PDF_BYTES = 30 * 1024 * 1024
KEY_PAGE_PATTERN = re.compile(
    r"turnover|revenue|profit and loss|income statement|balance sheet|statement of financial position|"
    r"employees|creditors|cash at bank|net assets|auditor", re.I)

FIGURE_FIELDS = ["revenue", "operating_profit", "profit_before_tax", "net_income", "cash", "total_debt",
                 "creditors_due_within_one_year", "creditors_due_after_one_year",
                 "total_assets_less_current_liabilities", "net_assets", "employee_count"]
AMOUNT_OWED_FIELDS = {"creditors_due_within_one_year", "creditors_due_after_one_year", "total_debt"}
FLOW_FIELDS = {"revenue", "operating_profit", "profit_before_tax", "net_income", "employee_count"}
HEADLINE = ["revenue", "operating_profit", "profit_before_tax", "net_income", "cash", "net_assets",
            "employee_count"]

SYSTEM = """You extract financial facts from a company's filed annual accounts for an M&A research tool.

Rules:
- Report only figures explicitly printed in the document for the CURRENT reporting period. Ignore comparative (prior-year) columns.
- Never estimate, infer, calculate or add figures together. If a figure is not explicitly printed, return null for it. Unknown is a correct and expected answer, especially for small or micro-entity accounts that omit the profit and loss account.
- If you are not sure a figure is the one asked for, return null.
- Copy evidence_text character-for-character from the page, including the label and the number as printed.
- page is the 1-based page number within the PDF you were given.
- Record the unit multiplier from the column heading (e.g. £'000 means 1000). Record the value exactly as printed before applying it.
- Use a negative value for profit or asset figures printed in brackets or described as a loss. Creditors and debt are amounts owed: report them as positive numbers even if the balance sheet prints them in brackets."""


class AIDocuments(Provider):
    name = "ai_documents"
    label = "AI: filed accounts (PDF)"

    def _candidates(self, ctx):
        docs = [s for s in ctx.sources
                if s.get("document_type") in DOCUMENT_TYPES and s.get("content_path")
                and s.get("parse_status") in ("not_parsed", "unreadable")]
        return sorted(docs, key=lambda s: (s.get("period_end") or s.get("published_on") or formatting.parse_date("1900-01-01")),
                      reverse=True)[:MAX_DOCUMENTS]

    def not_applicable_reason(self, ctx):
        if not self._candidates(ctx):
            return ("No PDF accounts awaiting extraction. Machine-readable (iXBRL) filings are already "
                    "read directly; run research first to retrieve filings.")
        return None

    def run(self, ctx):
        llm.require_key()
        result = ProviderResult()
        problems, extracted, reused = [], [], []

        for i, src in enumerate(self._candidates(ctx)):
            if src.get("ai_extracted_sha256") and src["ai_extracted_sha256"] == src.get("content_sha256"):
                reused.append(src["title"])
                if i == 0:
                    result.detail = self._disclosure(src, set(src.get("ai_fact_types") or []))
                continue
            try:
                facts, rejected, meta, doc_notes = self.extract_document(ctx, src)
            except (CredentialsMissing, RateLimited):
                raise                      # affects every document: stop and report
            except EnrichmentError as exc:
                problems.append(f"{src['title']}: {exc}")
                continue
            result.attach[src["id"]] = {"facts": facts, "content_sha256": src.get("content_sha256")}
            extracted.append(f"{src['title']}: {len(facts)} facts")
            result.detail.setdefault("documents", []).append({
                "title": src["title"], "facts": len(facts), "rejected": rejected, "model": meta["model"],
                "input_tokens": meta["input_tokens"], "output_tokens": meta["output_tokens"],
                "request_id": meta["request_id"], "notes": doc_notes,
            })
            if i == 0:
                result.detail.update(self._disclosure(src, {f.fact_type for f in facts}))

        if problems and not extracted and not reused:
            raise EnrichmentError("; ".join(problems))
        if problems:
            result.status = "partial"
        parts = extracted + [f"{t}: already extracted (unchanged)" for t in reused] + problems
        result.message = "; ".join(parts)
        return result

    # -- one document -------------------------------------------------------

    def extract_document(self, ctx, src):
        pdf = document_cache.load(src["content_path"])
        if pdf is None:
            raise EnrichmentError("Cached copy of the document is missing. Re-run research to retrieve it.")

        from pypdf import PdfReader, PdfWriter
        reader = PdfReader(io.BytesIO(pdf))
        page_texts = [(p.extract_text() or "") for p in reader.pages]
        has_text = sum(len(t.strip()) for t in page_texts) > 200

        # Which original pages we send (0-based). Long documents with a text
        # layer are trimmed to the pages that mention the figures we need.
        mapping = list(range(len(page_texts)))
        scope_note = None
        if len(mapping) > FULL_DOCUMENT_PAGES:
            if has_text:
                keep = [i for i in mapping if i < 4 or KEY_PAGE_PATTERN.search(page_texts[i])]
                mapping = keep[:SUBSET_MAX_PAGES]
                scope_note = f"Sent {len(mapping)} of {len(page_texts)} pages (those mentioning key financial terms)."
            else:
                mapping = mapping[:FULL_DOCUMENT_PAGES]
                scope_note = f"Scanned document: only the first {FULL_DOCUMENT_PAGES} of {len(page_texts)} pages examined."
            writer = PdfWriter()
            for i in mapping:
                writer.add_page(reader.pages[i])
            buf = io.BytesIO()
            writer.write(buf)
            pdf = buf.getvalue()
        if len(pdf) > MAX_PDF_BYTES:
            raise EnrichmentError("Document is too large to send for extraction (over 30 MB).")

        content = [
            {"type": "document",
             "source": {"type": "base64", "media_type": "application/pdf",
                        "data": base64.standard_b64encode(pdf).decode("ascii")},
             "title": src.get("title") or "Annual accounts"},
            {"type": "text", "text": (
                f"Company: {ctx.company['name']}\n"
                f"Document: {src.get('title')}\n"
                + (f"Expected period end (from the filing record): {src['period_end']}\n" if src.get("period_end") else "")
                + "Extract the figures described by the schema. Return null for any figure not explicitly printed.")},
        ]
        parsed, meta = llm.extract(SYSTEM, content, AccountsExtraction, task="deep_documents",
                                   company_id=ctx.company.get("id"))
        facts, rejected = self._to_facts(parsed, src, page_texts, mapping, meta["model"])
        notes = " ".join(n for n in (parsed.notes, scope_note) if n) or None
        return facts, rejected, meta, notes

    def _to_facts(self, parsed, src, page_texts, mapping, model):
        start = formatting.parse_date(parsed.period_start)
        end = formatting.parse_date(parsed.period_end) or src.get("period_end")
        facts, rejected = [], []

        def locate(page):
            if not 1 <= page <= len(mapping):
                return None, None
            original = mapping[page - 1]
            return original + 1, page_texts[original]

        for field in FIGURE_FIELDS:
            fig = getattr(parsed, field)
            if fig is None:
                continue
            page_no, text = locate(fig.page)
            reason = None
            if page_no is None:
                reason = f"cited page {fig.page} does not exist"
            elif not verify.number_in_quote(fig.value, fig.evidence_text):
                reason = "number not present in its quoted evidence"
            elif len(text.strip()) >= 50 and not verify.quote_in_text(fig.evidence_text, text):
                reason = f"quote not found on page {page_no}"
            if reason:
                rejected.append({"fact_type": field, "reason": reason, "page": page_no})
                continue

            verifiable = len(text.strip()) >= 50
            confidence = fig.confidence if verifiable or fig.confidence == "low" else "medium"
            printed = Decimal(str(fig.value))
            if field in AMOUNT_OWED_FIELDS:
                # Balance sheets print creditors in brackets because they are
                # deducted, not because they are negative: store the amount owed.
                printed = abs(printed)
            value = printed * fig.unit_multiplier
            if field == "employee_count":
                text_value, currency = f"{formatting.count(value)} (average)", None
            else:
                currency = (fig.currency or "").upper() or None
                text_value = formatting.money(value, currency)
            flow = field in FLOW_FIELDS
            if flow:
                period = formatting.period_label(start, end) if start else (
                    f"period ended {formatting.day(end)}" if end else None)
            else:
                period = f"as at {formatting.day(end)}" if end else None
            facts.append(Fact(
                fact_type=field, value_text=text_value, value_number=value, currency=currency,
                period=period, period_start=start if flow else None, period_end=end,
                evidence_type="ai_extracted", confidence=confidence,
                excerpt=formatting.squash(fig.evidence_text, 300),
                location=f"Page {page_no} of {src.get('title')}",
                evidence_page=page_no,
                notes=(f"Printed as {formatting.count(printed) if printed == printed.to_integral_value() else printed} in units of {fig.unit_multiplier:,}." if fig.unit_multiplier > 1 else None),
                extraction_method="llm_document", extraction_model=model,
                verification="quote_verified" if verifiable else "not_verifiable",
            ))

        if parsed.auditor is not None:
            page_no, text = locate(parsed.auditor.page)
            if page_no and (len(text.strip()) < 50 or verify.quote_in_text(parsed.auditor.evidence_text, text)):
                facts.append(Fact(
                    fact_type="auditor", value_text=formatting.squash(parsed.auditor.value, 120),
                    evidence_type="ai_extracted",
                    confidence=(parsed.auditor.confidence if len(text.strip()) >= 50 or parsed.auditor.confidence == "low"
                                else "medium"),
                    excerpt=formatting.squash(parsed.auditor.evidence_text, 300),
                    location=f"Page {page_no} of {src.get('title')}", evidence_page=page_no,
                    extraction_method="llm_document", extraction_model=model,
                    verification="quote_verified" if len(text.strip()) >= 50 else "not_verifiable",
                ))
            else:
                rejected.append({"fact_type": "auditor", "reason": "quote not found", "page": page_no})
        return facts, rejected

    def _disclosure(self, src, found):
        missing = [ft for ft in HEADLINE if ft not in found]
        if not missing:
            return {}
        return {"not_disclosed": missing, "not_disclosed_kind": "ai_checked",
                "not_disclosed_reason": f"Not found in {src.get('title')} (AI-read PDF)"}
