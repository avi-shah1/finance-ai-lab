"""Evidence links: jump to the quote, open PDFs at the page, and never
link Exa-profile facts to a website that doesn't contain them."""

import unittest
from urllib.parse import unquote

from links import evidence_href, external_url, text_fragment


class EvidenceLinkTests(unittest.TestCase):
    HOME = "https://www.ismiledental.co.uk/"

    def test_website_quote_scrolls_to_the_text(self):
        href = evidence_href({"source_url": self.HOME, "extraction_method": "llm_website",
                              "excerpt": "iSmile Dental Software Links with the majority of Xray kit/software and"})
        base, fragment = href.split("#:~:text=")
        self.assertEqual(base, self.HOME)
        start, end = fragment.split(",")
        self.assertEqual(unquote(start), "iSmile Dental Software Links with")
        self.assertEqual(unquote(end), "majority of Xray kit/software and")      # last 5 words

    def test_short_quote_is_used_whole_and_special_characters_encoded(self):
        self.assertEqual(text_fragment("Multi-site groups, NHS & private"),
                         "Multi%2Dsite%20groups%2C%20NHS%20%26%20private")

    def test_harvested_link_uses_its_label(self):
        href = evidence_href({"source_url": self.HOME, "extraction_method": "html_rule",
                              "excerpt": 'Link "Patient App" → https://www.ismiledental.co.uk/products/app'})
        self.assertTrue(href.endswith("#:~:text=Patient%20App"))

    def test_pdf_opens_at_page(self):
        href = evidence_href({"source_url": "https://example.gov/doc.pdf", "evidence_page": 14,
                              "extraction_method": "llm_document", "excerpt": "Turnover 47,310"})
        self.assertEqual(href, "https://example.gov/doc.pdf#page=14")

    def test_exa_profile_facts_get_no_link(self):
        self.assertEqual(evidence_href({"source_url": self.HOME, "extracted_by": "discovery",
                                        "excerpt": "employs 12 people"}), "")

    def test_filing_figures_link_to_the_filing_without_a_fragment(self):
        url = "https://www.sec.gov/Archives/edgar/data/1108524/crm-20260131.htm"
        self.assertEqual(evidence_href({"source_url": url, "extraction_method": "xbrl_tag", "excerpt": "x"}), url)

    def test_no_url_or_unsafe_url(self):
        self.assertEqual(evidence_href({"source_url": None}), "")
        self.assertEqual(evidence_href({"source_url": "javascript:alert(1)"}), "")
        self.assertEqual(external_url("acme.com"), "https://acme.com")

    def test_existing_fragment_replaced_and_ellipsis_dropped(self):
        href = evidence_href({"source_url": self.HOME + "#top", "extraction_method": "llm_website",
                              "excerpt": "Book appointments online…"})
        self.assertEqual(href, self.HOME + "#:~:text=Book%20appointments%20online")


if __name__ == "__main__":
    unittest.main()
