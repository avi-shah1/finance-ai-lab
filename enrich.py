"""Run research for a company from the command line.

Usage:  python enrich.py <company_id> [provider ...]
        python enrich.py 3                  # all providers
        python enrich.py 3 sec yfinance     # just these
"""

import sys

from enrichment.runner import enrich_company

if __name__ == "__main__":
    if len(sys.argv) < 2 or not sys.argv[1].isdigit():
        sys.exit(__doc__)
    for s in enrich_company(int(sys.argv[1]), only=sys.argv[2:] or None):
        print(f"{s['label']:28} {s['status']:20} {s['facts']:4} facts  {s['message']}")
