"""Claude's draft assessment of one candidate against the buyer.

Claude (Sonnet) sees only numbered evidence lines, e.g.

    F812 | Product: Patient Scheduling | Company source (accurx.com website)
    E57  | Fundraising, 2025-03-14: Raised £30m Series C | Third-party (sifted.eu): "..."

and must cite those IDs for every rating. Code then enforces the rules
rather than trusting the prompt:
  * a cited ID that wasn't in the evidence given is removed;
  * a criterion or dimension left with no valid citation becomes
    Insufficient Evidence, whatever rating Claude gave;
  * the overall view is only stored when at least one dimension has
    evidence behind it.
There are no numeric scores anywhere.
"""

import logging

import database
import screening
from enrichment import formatting, llm
from profiles import CompanyProfile

from .schemas import AssessmentResult

log = logging.getLogger(__name__)

PROMPT_VERSION = "assess-v3"   # v2: headline, strengths/risks/next checks, buyer brief; v3: no IDs in prose
BRIEF_ID = "BRIEF"             # evidence ID of the analyst's buyer brief
MAX_POINTS = 3
MAX_PER_TYPE = 12       # values per multi-valued fact type (e.g. products)
MAX_EVENTS = 15
EXCERPT_CHARS = 160

# Fact types that say nothing about fit or feasibility (filing admin, addresses).
SKIP_TYPES = {"officer", "registered_office", "accounts_next_due", "accounting_reference_date",
              "confirmation_statement_last", "confirmation_statement_next_due", "company_number",
              "registration_number"}
# The buyer's side only needs what it does, where, and its scale.
BUYER_TYPES = {"description", "product", "capability", "industry_served", "customer_segment",
               "notable_customer", "geography_presence", "strategic_priority", "integration",
               "revenue", "employee_count", "market_cap", "ownership_status", "headquarters"}

SYSTEM = """You assess a possible acquisition target for a named buyer, for an M&A screening tool used by analysts.

You get numbered evidence lines about the buyer and the target. Each line has an ID (F = fact, E = event), what it says, and its source type. There may also be a buyer brief (ID BRIEF): the analyst's description of the buyer's strategy and what it wants. Treat it as the buyer's stated intent, the best guide to strategic fit, but not as verified fact; cite BRIEF when you rely on it.

Rules:
- Use ONLY the evidence lines. Do not use outside knowledge about either company, even if you recognise them.
- Every rating must cite the IDs of the lines it relies on, in the evidence_ids fields. If no line addresses a criterion, rate it insufficient_evidence and cite nothing. Missing evidence is not evidence of weakness.
- Write all text (facts, interpretation, reasons, headline, strengths, risks, next checks) as plain prose for a reader: never put evidence IDs such as F120, E7 or BRIEF in the text; they belong only in evidence_ids.
- 'facts' restates what the cited lines say, with no judgement. 'interpretation' is your judgement for this buyer. Keep them separate.
- Weigh sources: filings and registers are strongest; the company's own website is a claim; third-party estimates (e.g. revenue estimates) and AI interpretations are weakest. Say when a rating rests on weak sources.
- The target already matches the search's sector and geography; judge how good a target it is, not whether it is in scope.
- Ratings: very_strong, strong, mixed, weak, insufficient_evidence. Overall: very_strong_fit, good_fit, mixed, weak_fit, avoid ('avoid' only for a clear evidenced blocker, e.g. already acquired, dissolved, far too large to buy).
- The overall headline gives the verdict and its deciding reason in one sentence. Strengths and risks are the few points that matter most, each backed by cited evidence where possible; next_checks are what an analyst should find out next.
- Be concise. No numbers you were not given."""


def assess_candidate(search, buyer_profile, company_id):
    """Assess one candidate, store the result, and return a short summary."""
    target = _profile(company_id)
    buyer_lines, buyer_ids = evidence_lines(buyer_profile, buyer=True)
    target_lines, target_ids = evidence_lines(target, buyer=False)
    valid = {**buyer_ids, **target_ids}
    brief = (search.get("buyer_brief") or "").strip()
    if brief:
        valid[BRIEF_ID] = ("brief", None)

    # The first block is identical for every candidate in a search (buyer,
    # search, criteria, brief), so it is cached: later calls pay ~10% for it.
    shared = (
        f"Buyer: {buyer_profile.company['name']}\n"
        f"Search: target sector '{search['target_sector']}', geography '{search['geography']}'"
        + (f", additional criteria: {search['criteria']}" if search.get("criteria") else "") + "\n\n"
        f"{_criteria_guide()}\n\n"
        + (f"<buyer_brief id=\"{BRIEF_ID}\">\n{brief}\n</buyer_brief>\n\n" if brief else "")
        + "<buyer_evidence>\n" + ("\n".join(buyer_lines) or "(none)") + "\n</buyer_evidence>")
    content = [
        {"type": "text", "text": shared, "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": (
            f"Target: {target.company['name']} "
            f"({target.company.get('domain') or target.company.get('website') or 'no website'})\n\n"
            "<target_evidence>\n" + ("\n".join(target_lines) or "(none)") + "\n</target_evidence>\n\n"
            "Assess the target for this buyer.")},
    ]

    result, meta = llm.extract(SYSTEM, content, AssessmentResult, task="assessment", max_tokens=16000,
                               search_id=search["id"], company_id=company_id)
    rows, dropped = to_rows(result, valid)
    database.save_ai_assessment(search["id"], company_id, meta["model"], PROMPT_VERSION, rows)
    overall = next((r["rating"] for r in rows if r["dimension"] == "overall"), None)
    return {"overall": overall, "rows": len(rows), "dropped_citations": dropped, "cost_usd": meta["cost_usd"]}


def _profile(company_id):
    company = database.get_company(company_id)
    return CompanyProfile(company, database.get_facts_for([company_id]).get(company_id, []),
                          database.get_events_for([company_id]).get(company_id, []))


def evidence_lines(profile, buyer):
    """Numbered evidence lines for the prompt, and {ID: ("fact"|"event", id)}."""
    lines, ids = [], {}
    types = sorted({f["fact_type"] for f in profile.facts} - SKIP_TYPES)
    if buyer:
        types = [t for t in types if t in BUYER_TYPES]
    for fact_type in types:
        values = profile.all(fact_type)
        # One preferred value for single-valued facts, a capped list for the rest.
        chosen = values[:MAX_PER_TYPE] if values[0]["multi_valued"] else [profile.fact(fact_type)]
        for f in chosen:
            key = f"F{f['id']}"
            ids[key] = ("fact", f["id"])
            lines.append(f"{key} | {f['label']}: {f['value_text']}{_when(f)} | {_source(f)}")
    if not buyer:
        for e in profile.events[:MAX_EVENTS]:
            key = f"E{e['id']}"
            ids[key] = ("event", e["id"])
            date = e["event_date"].isoformat() if e.get("event_date") else "date unknown"
            summary = f" ({e['summary']})" if e.get("summary") else ""
            lines.append(f"{key} | {screening.EVENT_TYPES[e['event_type']]}, {date}: {e['title']}{summary} | {_source(e)}")
    return lines, ids


def _when(f):
    if f.get("period"):
        return f" ({f['period']})"
    if f.get("as_of"):
        return f" (as of {f['as_of'].isoformat()})"
    return ""


def _source(item):
    kind = screening.EVIDENCE_TYPES[item["evidence_type"]]
    name = item.get("source_title") or item.get("source_name") or ""
    quote = f': "{formatting.squash(item["excerpt"], EXCERPT_CHARS)}"' if item.get("excerpt") else ""
    return f"{kind} ({formatting.squash(name, 60)}){quote}"


def _criteria_guide():
    parts = []
    for d in screening.DIMENSIONS:
        crit = "; ".join(f"{code} = {label}" for code, label in d["criteria"].items())
        parts.append(f"{d['code']} ({d['label']}): {crit}")
    return "Dimensions and criteria:\n" + "\n".join(parts)


def to_rows(result, valid):
    """Turn Claude's answer into assessment rows, enforcing the citation
    rules. Returns (rows, number of citations removed)."""
    rows, dropped = [], 0

    def keep(cited):
        nonlocal dropped
        good = [c.strip().upper() for c in cited if c.strip().upper() in valid]
        dropped += len(cited) - len(good)
        return list(dict.fromkeys(good))   # de-duplicate, keep order

    def split(cited):
        return ([valid[c][1] for c in cited if valid[c][0] == "fact"],
                [valid[c][1] for c in cited if valid[c][0] == "event"])

    evidenced_dimensions = 0
    for d in screening.DIMENSIONS:
        dim = getattr(result, d["code"])
        dim_cited = keep(dim.evidence_ids)
        seen = set()
        for c in dim.criteria:
            code = c.criterion.strip().lower()
            if code not in d["criteria"] or code in seen:
                continue            # not a criterion of this dimension, or a repeat
            seen.add(code)
            cited = keep(c.evidence_ids)
            dim_cited += [x for x in cited if x not in dim_cited]
            rating = c.rating if cited else "insufficient_evidence"
            reason = formatting.squash(c.reason, 300)
            if not cited and c.rating != "insufficient_evidence":
                reason = "No valid evidence was cited, so this is marked insufficient. " + reason
            facts, events = split(cited)
            rows.append({"dimension": d["code"], "criterion": code, "rating": rating,
                         "rationale": reason, "fact_ids": facts, "event_ids": events,
                         "cites_brief": BRIEF_ID in cited})
        rating = dim.rating if dim_cited else "insufficient_evidence"
        if dim_cited and rating != "insufficient_evidence":
            evidenced_dimensions += 1
        facts, events = split(dim_cited)
        rows.append({"dimension": d["code"], "criterion": None, "rating": rating,
                     "rationale": formatting.squash(dim.interpretation, 800),
                     "facts_summary": formatting.squash(dim.facts, 800),
                     "interpretation": formatting.squash(dim.interpretation, 800),
                     "fact_ids": facts, "event_ids": events, "cites_brief": BRIEF_ID in dim_cited})

    if evidenced_dimensions:
        o = result.overall
        cited = keep(o.evidence_ids)
        # A strength must rest on cited evidence; a risk may be a gap in it.
        strengths = [p for p in (_point(p, keep) for p in o.strengths) if p["evidence_ids"]][:MAX_POINTS]
        risks = [_point(p, keep) for p in o.risks][:MAX_POINTS]
        for p in strengths + risks:
            cited += [x for x in p["evidence_ids"] if x not in cited]
        facts, events = split(cited)
        rows.append({"dimension": "overall", "criterion": None, "rating": o.rating,
                     "headline": formatting.squash(o.headline, 250),
                     "rationale": formatting.squash(o.rationale, 800),
                     "interpretation": formatting.squash(o.rationale, 800),
                     "highlights": {"strengths": strengths, "risks": risks,
                                    "next_checks": [formatting.squash(q, 200) for q in o.next_checks
                                                    if q.strip()][:MAX_POINTS]},
                     "fact_ids": facts, "event_ids": events, "cites_brief": BRIEF_ID in cited})
    return rows, dropped


def _point(point, keep):
    return {"text": formatting.squash(point.text, 200), "evidence_ids": keep(point.evidence_ids)}
