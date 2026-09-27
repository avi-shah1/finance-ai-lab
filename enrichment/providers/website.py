"""The company's own website, treated as a primary source for how the
company describes itself (evidence type `company_reported`).

Deliberately conservative and deterministic, with no AI:
  * description   <- the homepage's meta description (the company's own words)
  * organisation  <- schema.org JSON-LD the site publishes about itself
  * products, solutions, industries, customers, integrations
                  <- links the site makes to its own item pages, e.g. a link
                     labelled "Patient Scheduling" pointing to /products/scheduling.
                     The link label is the fact; the page it appears on is the source.
  * annual reports <- PDF links labelled "annual report" on the homepage or
                     investor-relations page (metadata only; not parsed yet).

Reading meaning out of marketing copy (e.g. "which customer segments does
this imply?") is left to a later AI-assisted step.

Politeness: honours robots.txt, fetches at most MAX_PAGES pages, pauses
between requests.
"""

import json
import re
import time
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

from bs4 import BeautifulSoup

from .. import document_cache, formatting
from ..base import EnrichmentError, Fact, Provider, ProviderResult, SourceDoc, SourceUnavailable

MAX_PAGES = 8
PAUSE_SECONDS = 0.5

# first path segment -> fact type for links to individual item pages
SECTIONS = {
    "product": "product", "products": "product", "platform": "product",
    "solution": "capability", "solutions": "capability", "capabilities": "capability", "features": "capability",
    "industries": "industry_served", "industry": "industry_served", "sectors": "industry_served",
    "verticals": "industry_served",
    "customers": "notable_customer", "customer-stories": "notable_customer", "case-studies": "notable_customer",
    "success-stories": "notable_customer", "customer-success": "notable_customer",
    "integrations": "integration", "integration": "integration", "apps": "integration",
}
# section index pages worth fetching to harvest more item links
EXTRA_PAGES = {"about", "about-us", "company", "investors", "investor-relations", "ir"}
LOCALE = re.compile(r"^[a-z]{2}([-_][a-z]{2})?$", re.I)
GENERIC_LABELS = {
    "learn more", "read more", "see more", "see all", "view all", "explore", "overview", "more",
    "get started", "contact", "contact us", "pricing", "demo", "request a demo", "book a demo",
    "read the story", "read story", "read case study", "watch video", "find out more", "discover",
    "all products", "all solutions", "all industries", "all integrations", "view case study",
    "customer stories", "case studies", "products", "solutions", "industries", "integrations",
    "customers", "platform", "features", "try for free", "sign up", "log in", "login", "watch now",
}


class Website(Provider):
    name = "website"
    label = "Company website"

    def not_applicable_reason(self, ctx):
        if ctx.company.get("website"):
            return None
        return "No website on file. Add one (or run SEC/yfinance, which may supply it)."

    def run(self, ctx):
        self.http = ctx.http
        home_url = _normalise_url(ctx.company["website"])
        if not home_url:
            raise EnrichmentError(f"'{ctx.company['website']}' is not a usable website address.")
        self.robots = self._robots(home_url)
        if not self._allowed(home_url):
            raise SourceUnavailable("The website's robots.txt disallows automated access to its homepage.")

        home = self._fetch(home_url)
        base_host = _host(home.url)
        result = ProviderResult()
        home_src = self._page_source(home.url, home.soup)
        result.sources.append(home_src)
        self._describe(home_src, home.soup)

        pages = [(home.url, home.soup, home_src)]
        to_visit = self._section_pages(home.url, home.soup, base_host)
        for url in to_visit[: MAX_PAGES - 1]:
            if not self._allowed(url):
                continue
            time.sleep(PAUSE_SECONDS)
            try:
                page = self._fetch(url)
            except EnrichmentError:
                continue
            src = self._page_source(page.url, page.soup)
            result.sources.append(src)
            pages.append((page.url, page.soup, src))

        seen = set()
        for url, soup, src in pages:
            self._harvest_items(src, url, soup, base_host, seen)
            result.sources.extend(self._annual_report_links(url, soup))

        text_len = len(home.soup.get_text(" ", strip=True))
        n = sum(len(s.facts) for s in result.sources)
        result.message = f"{len(pages)} pages read from {base_host}: {n} facts."
        if text_len < 300:
            result.status = "partial"
            result.message += " The homepage has very little readable text (it may need JavaScript to render)."
        return result

    # -- fetching ---------------------------------------------------------

    def _fetch(self, url):
        resp = self.http.get(url, headers={"Accept": "text/html,application/xhtml+xml"})
        if "html" not in resp.headers.get("Content-Type", "html"):
            raise SourceUnavailable(f"{url} is not an HTML page.")
        resp.soup = BeautifulSoup(resp.content, "lxml")
        return resp

    def _robots(self, home_url):
        rp = RobotFileParser()
        try:
            text = self.http.get(urljoin(home_url, "/robots.txt")).text
            rp.parse(text.splitlines())
        except EnrichmentError:
            rp.parse([])   # no robots.txt: everything allowed
        return rp

    def _allowed(self, url):
        return self.robots.can_fetch(self.http.session.headers["User-Agent"], url)

    # -- extraction -------------------------------------------------------

    def _page_source(self, url, soup):
        title = soup.title.get_text(strip=True) if soup.title else None
        src = SourceDoc(provider=self.name, external_id=url, source_type="company_website",
                        name=f"{_host(url)} website", title=formatting.squash(title, 150) if title else None,
                        url=url)
        # Cache the page's visible text for AI extraction (ai_website.py).
        text = visible_text(soup)
        if text:
            src.content_path, src.content_sha256 = document_cache.save(self.name, text.encode("utf-8"), ".txt")
        return src

    def _describe(self, src, soup):
        meta = (soup.find("meta", attrs={"name": re.compile("^description$", re.I)})
                or soup.find("meta", attrs={"property": "og:description"}))
        content = formatting.squash(meta.get("content"), 500) if meta else ""
        if len(content) >= 20:
            src.facts.append(Fact(fact_type="description", value_text=content, evidence_type="company_reported",
                                  confidence="high", excerpt=content, location="Homepage meta description"))

        for org in _json_ld_organisations(soup):
            def add(fact_type, value, where):
                if isinstance(value, str) and value.strip():
                    src.facts.append(Fact(fact_type=fact_type, value_text=formatting.squash(value, 200),
                                          evidence_type="company_reported", confidence="medium",
                                          location=f"Structured data (schema.org {where})"))
            add("legal_name", org.get("legalName"), "legalName")
            if isinstance(org.get("foundingDate"), str) and re.match(r"^\d{4}", org["foundingDate"]):
                add("founded_year", org["foundingDate"][:4], "foundingDate")
            addr = org.get("address")
            if isinstance(addr, dict):
                add("headquarters", ", ".join(str(addr[k]) for k in ("addressLocality", "addressCountry")
                                              if isinstance(addr.get(k), str)), "address")
            areas = org.get("areaServed")
            for area in areas if isinstance(areas, list) else [areas]:
                name = area.get("name") if isinstance(area, dict) else area
                add("geography_presence", name, "areaServed")

    def _section_pages(self, home_url, soup, base_host):
        """Index pages (e.g. /products, /industries, /about) linked from the homepage."""
        urls = []
        for a in soup.find_all("a", href=True):
            url = urljoin(home_url, a["href"]).split("#")[0].split("?")[0]
            if _host(url) != base_host:
                continue
            segs = _segments(url)
            if len(segs) == 1 and (segs[0] in SECTIONS or segs[0] in EXTRA_PAGES) and url not in urls:
                urls.append(url)
        return urls

    def _harvest_items(self, src, page_url, soup, base_host, seen):
        for a in soup.find_all("a", href=True):
            url = urljoin(page_url, a["href"]).split("#")[0].split("?")[0]
            if _host(url) != base_host:
                continue
            segs = _segments(url)
            if len(segs) < 2 or segs[0] not in SECTIONS:
                continue
            label = formatting.squash(a.get_text(" ", strip=True), 80)
            if not _is_item_label(label, url):
                continue
            fact_type = SECTIONS[segs[0]]
            key = (fact_type, label.lower())
            if key in seen:
                continue
            seen.add(key)
            src.facts.append(Fact(
                fact_type=fact_type, value_text=label, evidence_type="company_reported", confidence="medium",
                excerpt=f'Link "{label}" → {url}', location=f"Link on {page_url}",
            ))

    def _annual_report_links(self, page_url, soup):
        docs = []
        for a in soup.find_all("a", href=True):
            label = formatting.squash(a.get_text(" ", strip=True), 120)
            url = urljoin(page_url, a["href"])
            if not re.search(r"annual\s+report", label, re.I) or not url.lower().split("?")[0].endswith(".pdf"):
                continue
            if not url.startswith(("http://", "https://")):
                continue
            year = re.search(r"(20\d\d)", label + " " + url)
            docs.append(SourceDoc(
                provider=self.name, external_id=url, source_type="annual_report", name=f"{_host(page_url)} website",
                title=label, url=url, document_type="annual_report", parse_status="not_parsed",
                notes=f"Linked from {page_url}" + (f"; year {year.group(1)}" if year else "")
                      + ". Not downloaded or parsed yet.",
            ))
        return docs


# ---------------------------------------------------------------------------

def visible_text(soup):
    """The human-readable text of a page, one block per line, without
    scripts, styles or hidden elements. Anchors are kept for harvesting
    because this works on a copy of the page."""
    copy = BeautifulSoup(str(soup), "lxml")
    for tag in copy(["script", "style", "noscript", "svg", "template", "iframe"]):
        tag.decompose()
    lines = [re.sub(r"\s+", " ", line).strip() for line in copy.get_text("\n").splitlines()]
    return "\n".join(line for line in lines if line)


def _normalise_url(website):
    website = (website or "").strip()
    if not website:
        return None
    if not re.match(r"^https?://", website, re.I):
        website = "https://" + website
    parsed = urlparse(website)
    return f"{parsed.scheme}://{parsed.netloc}/" if parsed.netloc and "." in parsed.netloc else None


def _host(url):
    host = urlparse(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def _segments(url):
    segs = [s.lower() for s in urlparse(url).path.split("/") if s]
    if (len(segs) > 1 and LOCALE.match(segs[0]) and len(segs[0]) in (2, 5)
            and segs[0] not in SECTIONS and segs[0] not in EXTRA_PAGES):
        segs = segs[1:]     # drop a locale prefix like /en/ or /en-gb/
    return segs


ACTION_WORDS = {"learn", "read", "view", "see", "explore", "watch", "try", "get", "start", "discover",
                 "find", "join", "book", "request", "download", "meet", "what", "how", "why", "contact"}
STOPWORDS = {"the", "and", "for", "of", "a", "an", "to", "in", "on", "with", "by", "your", "our"}


def _is_item_label(label, url):
    """A link label counts as a product/industry/customer name only if it
    looks like a name (capitalised, short, not a call to action) AND shares a
    word with the page it links to, e.g. "Sales Cloud" -> /products/sales-cloud.
    This keeps marketing phrases like "Watch all demos" out."""
    if not (2 <= len(label) <= 60) or len(label.split()) > 6:
        return False
    if not (label[0].isupper() or label[0].isdigit()):
        return False
    low = label.lower().strip(" →›>»")
    words = [w for w in re.findall(r"[a-z0-9]+", low) if w not in STOPWORDS]
    if low in GENERIC_LABELS or not words or words[0] in ACTION_WORDS:
        return False
    long_words = [w for w in label.split() if len(w) > 3]
    if long_words and sum(w[0].isupper() or w[0].isdigit() for w in long_words) * 2 < len(long_words):
        return False   # reads like a sentence, not a name
    slug_words = set(re.findall(r"[a-z0-9]+", _segments(url)[-1]))
    return any(w in slug_words or w.rstrip("s") in slug_words for w in words)


def _json_ld_organisations(soup):
    orgs = []
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except (ValueError, TypeError):
            continue
        items = data if isinstance(data, list) else data.get("@graph", [data]) if isinstance(data, dict) else []
        for item in items:
            if isinstance(item, dict):
                kind = item.get("@type")
                kinds = kind if isinstance(kind, list) else [kind]
                if any(k in ("Organization", "Corporation") for k in kinds):
                    orgs.append(item)
    return orgs
