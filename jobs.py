"""Run a search's pipeline in a background thread, so the web request that
started it can return immediately and the page can show progress.

This is deliberately simple: one thread per search inside the Flask
process. If the app restarts mid-search (e.g. the dev server reloading
after a code change), the thread dies with it; fail_interrupted() marks
such searches as failed at startup so they can be run again.
"""

import logging
import threading

import database
from discovery.runner import run_discovery
from enrichment.base import EnrichmentError

log = logging.getLogger(__name__)

_lock = threading.Lock()
_running = set()   # search ids with a live thread in this process


def start_search(search_id):
    """Start the pipeline for a search unless it is already running here.
    Returns True if a new job was started."""
    with _lock:
        if search_id in _running:
            return False
        _running.add(search_id)
    database.set_search_status(search_id, "queued", "Waiting to start")
    threading.Thread(target=_run, args=(search_id,), name=f"search-{search_id}", daemon=True).start()
    return True


def is_running(search_id):
    with _lock:
        return search_id in _running


def run_now(search_id):
    """Run the pipeline in the current thread (used by discover.py)."""
    with _lock:
        _running.add(search_id)
    return _run(search_id)


def _run(search_id):
    try:
        database.set_search_status(search_id, "discovering", "Starting")
        summary = run_discovery(search_id)
        # Screening (Level 2) will run here once it exists.
        database.set_search_status(search_id, "complete",
                                   f"{summary['relevant']} relevant companies found")
        return summary
    except EnrichmentError as exc:
        database.set_search_status(search_id, "failed", error=str(exc))
    except Exception as exc:  # a bug: keep the details in the log, show a short message
        log.exception("Search %s failed", search_id)
        database.set_search_status(search_id, "failed", error=f"Unexpected error: {type(exc).__name__}: {exc}")
    finally:
        with _lock:
            _running.discard(search_id)


def fail_interrupted():
    count = database.fail_unfinished_searches(
        "Interrupted: the app restarted while this search was running. Click 'Run again'.")
    if count:
        log.warning("Marked %s interrupted search(es) as failed", count)
