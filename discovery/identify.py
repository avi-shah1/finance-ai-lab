"""Group raw search results into candidate companies. Plain Python, no AI.

One domain = one company. Results from directories, social networks, news
sites and similar are dropped (they describe companies, they aren't them),
and so is the buyer itself.
"""

import re
from dataclasses import dataclass, field

from domains import normalise_domain
from enrichment import formatting

from .sources.base import SearchHit

# Sites that list or write about companies rather than being one.
NOT_COMPANIES = {
    "linkedin.com", "crunchbase.com", "wikipedia.org", "facebook.com", "twitter.com", "x.com",
    "youtube.com", "instagram.com", "tiktok.com", "medium.com", "substack.com", "github.com",
    "glassdoor.com", "glassdoor.co.uk", "indeed.com", "welcometothejungle.com", "apple.com", "google.com",
    "bloomberg.com", "reuters.com", "ft.com", "forbes.com", "techcrunch.com", "sifted.eu",
    "businesswire.com", "prnewswire.com", "globenewswire.com", "eu-startups.com",
    "pitchbook.com", "zoominfo.com", "dnb.com", "owler.com", "tracxn.com", "cbinsights.com",
    "dealroom.co", "f6s.com", "g2.com", "capterra.com", "capterra.co.uk", "getapp.com",
    "producthunt.com", "angel.co", "wellfound.com", "company-information.service.gov.uk",
    "gov.uk", "europa.eu", "yahoo.com", "reddit.com", "quora.com",
    "exa.ai",   # Exa's own profile pages: no company website to identify the company by
}
TITLE_SEPARATORS = re.compile(r"\s+[|\-–—:·•]\s+")


@dataclass
class Candidate:
    domain: str
    website: str
    name: str
    hits: list = field(default_factory=list)

    @property
    def queries(self):
        return {h.query for h in self.hits}

    @property
    def best_rank(self):
        return min(h.rank for h in self.hits)

    @property
    def best_hit(self):
        """The hit with the most page text (what triage reads)."""
        return max(self.hits, key=lambda h: (len(h.text or ""), -h.rank))

    @property
    def sources(self):
        return sorted({h.source for h in self.hits})


def group_hits(hits: list[SearchHit], buyer_name=None, buyer_domain=None):
    """Return (candidates, rejected) where rejected is [(hit, reason)].
    Candidates are ordered by how many queries found them, then by rank."""
    buyer_key = re.sub(r"[^a-z0-9]", "", formatting.normalise_name(buyer_name))
    by_domain, rejected = {}, []
    for hit in hits:
        domain = normalise_domain(hit.url)
        reason = _reject_reason(domain, buyer_key, buyer_domain)
        if reason:
            rejected.append((hit, reason))
            continue
        cand = by_domain.get(domain)
        if cand is None:
            cand = by_domain[domain] = Candidate(domain=domain, website=f"https://{domain}",
                                                 name=name_from_title(hit.title, domain))
        cand.hits.append(hit)
    candidates = sorted(by_domain.values(), key=lambda c: (-len(c.queries), c.best_rank, c.domain))
    return candidates, rejected


def _reject_reason(domain, buyer_key, buyer_domain):
    if not domain:
        return "not a usable web address"
    if domain in NOT_COMPANIES:
        return "directory, social or news site"
    if buyer_domain and domain == buyer_domain:
        return "the buyer's own site"
    # e.g. buyer "Salesforce" -> salesforce.com, salesforceventures.com. Short
    # names only match exactly, so buyer "Sage" doesn't exclude sagehealth.com.
    label = domain.split(".")[0]
    if buyer_key and (label == buyer_key or (len(buyer_key) >= 6 and label.startswith(buyer_key))):
        return "the buyer's own site"
    return None


def name_from_title(title, domain):
    """'Accurx | Healthcare communication' -> 'Accurx'. Falls back to the domain."""
    if title:
        first = TITLE_SEPARATORS.split(title.strip())[0].strip()
        if 1 < len(first) <= 60:
            return first
    return domain.split(".")[0].capitalize()
