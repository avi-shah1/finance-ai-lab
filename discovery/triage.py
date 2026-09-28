"""Cheap first judgement on each discovered company, from the text the
search source returned (no extra fetching).

Claude reads each candidate's text and says what the company does and
whether it fits the sector and geography, and why. Size and location are
not asked for: profile.py reads them from the text without AI.
Batches run in parallel, so a search waits for one Claude call, not several.
"""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from enrichment import formatting, llm

from .schemas import TriageResult

BATCH_SIZE = 20          # candidates per Claude call
PARALLEL_CALLS = 3
TEXT_CHARS = 1000        # text per candidate

SYSTEM = """You screen companies found by a web search for an M&A target-screening tool. For each candidate you get its domain, title and the start of a company description (often a data provider's profile of the company).

Rules:
- Judge ONLY from the text given. Do not use outside knowledge about any company, even if you recognise it.
- Interpret regions geographically, not politically: Europe includes the UK, Switzerland and Norway. Only apply a narrower reading (e.g. EU members only) when the additional criteria ask for it.
- If the text doesn't say where the company is based or operates, geography_fit is 'unclear', not 'no'.
- A company that serves the target sector only as one of many unrelated markets is at most 'partial'.
- summary: 2-3 neutral third-person bullets on what the company does (never "you"/"we" or marketing phrases). match_reason: one short factual sentence.
- Return one item for every candidate id."""


@dataclass
class Verdict:
    name: str
    in_scope: bool
    relevance: str | None       # high / medium / low; None when out of scope
    summary: str | None         # 2-3 bullets, newline-separated
    match_reason: str
    model: str


def triage(candidates, sector, geography, criteria=None, search_id=None):
    """Return {domain: Verdict} for every candidate Claude answered for."""
    batches = [candidates[i:i + BATCH_SIZE] for i in range(0, len(candidates), BATCH_SIZE)]
    verdicts = {}
    with ThreadPoolExecutor(max_workers=PARALLEL_CALLS) as pool:
        for result in pool.map(lambda b: _triage_batch(b, sector, geography, criteria, search_id), batches):
            verdicts.update(result)
    return verdicts


def _triage_batch(batch, sector, geography, criteria, search_id):
    ids = {f"C{i}": c for i, c in enumerate(batch, start=1)}
    blocks = [
        f'<candidate id="{cid}" domain="{c.domain}">\n'
        f"Title: {c.best_hit.title or ''}\n{(c.best_hit.text or '').strip()[:TEXT_CHARS]}\n</candidate>"
        for cid, c in ids.items()
    ]
    content = [{"type": "text", "text": (
        f"Target sector: {sector}\nGeography: {geography}\nAdditional criteria: {criteria or 'none'}\n\n"
        + "\n\n".join(blocks))}]
    result, meta = llm.extract(SYSTEM, content, TriageResult, task="triage", max_tokens=12000,
                               search_id=search_id)

    verdicts = {}
    for item in result.items:
        cand = ids.get(item.candidate_id)
        if cand is None or cand.domain in verdicts:
            continue
        in_scope = (item.is_operating_company and item.relevance != "none"
                    and item.sector_fit in ("yes", "partial") and item.geography_fit != "no")
        verdicts[cand.domain] = Verdict(
            name=formatting.squash(item.company_name, 80) or cand.name,
            in_scope=in_scope,
            relevance=item.relevance if in_scope else None,
            summary=formatting.clean_bullets(item.summary),
            match_reason=formatting.squash(item.match_reason, 300),
            model=meta["model"],
        )
    return verdicts
