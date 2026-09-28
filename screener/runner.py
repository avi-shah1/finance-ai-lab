"""Screen a search's most promising candidates: gather Level 2 evidence and
get Claude's assessment for each, several companies at a time.

A failure for one company is recorded on that candidate (screening_error)
and the others carry on.
"""

import logging
import threading
from concurrent.futures import ThreadPoolExecutor

import database
from profiles import CompanyProfile

from . import assess, buyer, evidence

log = logging.getLogger(__name__)

SCREEN_LIMIT = 20     # candidates screened automatically per search
PARALLEL = 4          # companies screened at the same time


def run_screening(search_id, company_ids=None, gather=True):
    """Screen the top SCREEN_LIMIT discovered candidates, or exactly
    `company_ids` when given. gather=False re-runs only the assessment on
    the evidence already held (e.g. after the buyer brief changed).
    Returns a summary dict."""
    search = database.get_search(search_id)
    status = lambda message: database.set_search_status(search_id, "screening", message)
    verb = "Screening" if gather else "Re-assessing"

    if gather:
        status("Profiling the buyer")
        database.add_search_decision(search_id, "buyer", "profile",
                                     buyer.ensure_buyer_profile(search["buyer_company_id"], search_id))
    buyer_profile = _profile(search["buyer_company_id"])

    candidates = database.get_candidates(search_id)
    if company_ids is not None:
        todo = [c for c in candidates if c["id"] in set(company_ids)]
    else:
        todo = [c for c in candidates if c["stage"] == "discovered"][:SCREEN_LIMIT]
    if not todo:
        return {"screened": 0, "failed": 0}

    lock, progress = threading.Lock(), {"done": 0, "failed": 0}
    status(f"{verb} {len(todo)} candidates (0 done)")

    def screen_one(candidate):
        try:
            if gather:
                evidence.gather(candidate["id"], search_id=search_id)
            assess.assess_candidate(search, buyer_profile, candidate["id"])
            database.mark_screened(search_id, candidate["id"])
        except Exception as exc:
            log.exception("Screening failed for company %s in search %s", candidate["id"], search_id)
            database.mark_screened(search_id, candidate["id"], error=f"{type(exc).__name__}: {exc}"[:500])
            with lock:
                progress["failed"] += 1
        finally:
            with lock:
                progress["done"] += 1
                done = progress["done"]
            status(f"{verb} {len(todo)} candidates ({done} done)")

    with ThreadPoolExecutor(max_workers=PARALLEL) as pool:
        list(pool.map(screen_one, todo))

    screened = progress["done"] - progress["failed"]
    database.add_search_decision(
        search_id, "screening" if gather else "re-assessment", "complete",
        f"{screened} of {len(todo)} candidates " + ("screened and assessed" if gather else "re-assessed")
        + (f"; {progress['failed']} failed (see each candidate)" if progress["failed"] else ""))
    return {"screened": screened, "failed": progress["failed"]}


def _profile(company_id):
    company = database.get_company(company_id)
    return CompanyProfile(company, database.get_facts_for([company_id]).get(company_id, []),
                          database.get_events_for([company_id]).get(company_id, []))
