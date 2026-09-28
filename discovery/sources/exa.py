"""Exa (exa.ai): search by meaning. With category "company" it returns
company homepages, which makes each result one candidate company.

API: POST https://api.exa.ai/search with header x-api-key.
Note: the company category rejects excludeDomains and date filters, so
unwanted sites are filtered in identify.py instead.
"""

import os
from decimal import Decimal

import usage
from enrichment.base import CredentialsMissing
from enrichment.http import Http

from .base import SearchHit, SearchSource

ENDPOINT = "https://api.exa.ai/search"
TEXT_CHARS = 1000   # first part of each page's text: enough to see what the company does


class ExaSource(SearchSource):
    name = "exa"

    def __init__(self, http=None):
        self.http = http or Http()

    def available(self):
        return bool(os.environ.get("EXA_API_KEY"))

    def search(self, query, *, category="company", num_results=10, search_id=None, task="discovery_search"):
        key = os.environ.get("EXA_API_KEY")
        if not key:
            raise CredentialsMissing("API credentials required: add EXA_API_KEY to .env.")
        payload = {
            "query": query,
            "type": "auto",
            "category": category,
            "numResults": num_results,
            "contents": {"text": {"maxCharacters": TEXT_CHARS}},
        }
        data = self.http.post_json(ENDPOINT, payload, headers={"x-api-key": key})

        results = data.get("results") or []
        # Exa reports what the call actually cost; fall back to our estimate.
        cost = (data.get("costDollars") or {}).get("total")
        usage.record_service("exa", task, "search", 1, search_id=search_id,
                             cost_usd=Decimal(str(cost)) if cost is not None else None)
        if cost is None and results:
            usage.record_service("exa", task + "_text", "page", len(results), search_id=search_id)

        return [
            SearchHit(source=self.name, query=query, rank=i, url=r["url"], title=r.get("title"),
                      text=r.get("text"), published=r.get("publishedDate"))
            for i, r in enumerate(results, start=1) if r.get("url")
        ]
