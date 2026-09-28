"""What every web-search source returns, so the rest of discovery doesn't
care which API found a result. To add a source (e.g. Brave), subclass
SearchSource and return SearchHits."""

from dataclasses import dataclass


@dataclass
class SearchHit:
    source: str                 # exa / brave
    query: str
    rank: int                   # 1 = first result for that query
    url: str
    title: str | None = None
    text: str | None = None     # page text or snippet returned by the source
    published: str | None = None


class SearchSource:
    name = ""

    def available(self):
        """False when the API key is missing."""
        raise NotImplementedError

    def search(self, query, *, category="company", num_results=10, search_id=None, task="discovery_search"):
        """Return a list of SearchHit. Records its own cost in api_usage."""
        raise NotImplementedError
