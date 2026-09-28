"""Run target discovery for an existing search from the command line
(useful for testing without the web app).

Usage:  python discover.py <search_id>
"""

import logging
import sys

import database
import jobs

if __name__ == "__main__":
    if len(sys.argv) != 2 or not sys.argv[1].isdigit():
        sys.exit(__doc__)
    logging.basicConfig(level=logging.INFO)
    search_id = int(sys.argv[1])
    summary = jobs.run_now(search_id)
    search = database.get_search(search_id)
    print(f"Status: {search['status']}  {search['status_message'] or ''}{search['error'] or ''}")
    for d in search["decisions"]:
        print(f"  [{d['step']}] {d['decision']}: {d['reason']}")
    for row in database.get_usage_summary(search_id=search_id):
        print(f"  cost {row['service']:9} {row['task']:22} {row['calls']:3} calls  ${row['cost_usd'] or 0:.4f}")
