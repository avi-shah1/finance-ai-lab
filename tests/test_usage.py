"""Offline tests for model selection and cost estimates (no API calls)."""

import os
import unittest
from decimal import Decimal
from unittest import mock

import usage
from enrichment import llm


class ModelSelectionTests(unittest.TestCase):
    @mock.patch.dict(os.environ, {}, clear=True)
    def test_defaults(self):
        self.assertEqual(llm.model_for("triage"), "claude-haiku-4-5")
        self.assertEqual(llm.model_for("deep_documents"), "claude-haiku-4-5")
        self.assertEqual(llm.model_for("assessment"), "claude-sonnet-5")

    @mock.patch.dict(os.environ, {"ANTHROPIC_MODEL_DEEP_DOCUMENTS": "claude-sonnet-5"}, clear=True)
    def test_env_override(self):
        self.assertEqual(llm.model_for("deep_documents"), "claude-sonnet-5")
        self.assertEqual(llm.model_for("triage"), "claude-haiku-4-5")

    def test_unknown_task_is_an_error(self):
        with self.assertRaises(ValueError):
            llm.model_for("tirage")


class CostTests(unittest.TestCase):
    def test_haiku(self):
        # 6,000 in x $1/M + 1,000 out x $5/M = $0.006 + $0.005
        self.assertEqual(usage.claude_cost("claude-haiku-4-5", 6000, 1000), Decimal("0.011"))

    def test_sonnet(self):
        # 4,000 in x $2/M + 1,500 out x $10/M = $0.008 + $0.015
        self.assertEqual(usage.claude_cost("claude-sonnet-5", 4000, 1500), Decimal("0.023"))

    def test_dated_model_id_matches(self):
        self.assertEqual(usage.claude_cost("claude-haiku-4-5-20251001", 1_000_000, 0), Decimal("1"))

    def test_cache_pricing(self):
        # 1M cache writes at 1.25x and 1M cache reads at 0.1x of $2/M
        self.assertEqual(usage.claude_cost("claude-sonnet-5", 0, 0, cache_read_tokens=1_000_000,
                                           cache_write_tokens=1_000_000), Decimal("2.70"))

    def test_unknown_model_has_no_price(self):
        self.assertIsNone(usage.claude_cost("some-other-model", 100, 100))

    def test_recording_failure_does_not_raise(self):
        with mock.patch("database.insert_api_usage", side_effect=RuntimeError("db down")), \
             self.assertLogs("usage", level="ERROR"):
            usage.record_service("exa", "discovery_search", "search", 1)


class StripRefsTests(unittest.TestCase):
    def test_examples(self):
        from enrichment.formatting import strip_refs
        cases = {
            "serving small and corporate dental practices in the UK (F988, F1036, F1037, F1038). Salesforce is":
                "serving small and corporate dental practices in the UK. Salesforce is",
            "no listed dental-specific vertical (F751, F782, F781, F784).": "no listed dental-specific vertical.",
            "clinical modules (F847, F853-F861), based in Munich": "clinical modules, based in Munich",
            "investments from SAP (E13) and Fresenius (E14) in 2026": "investments from SAP and Fresenius in 2026",
            "owned by Henry Schein One (75-100% per the PSC register)":
                "owned by Henry Schein One (75-100% per the PSC register)",       # real brackets kept
            "matches the stated strategy (BRIEF).": "matches the stated strategy.",
            "growth of 106.6% YoY per F475, strong": "growth of 106.6% YoY, strong",
            "Supports F-Gas compliance and Series E funding": "Supports F-Gas compliance and Series E funding",
            "": "", None: None,
        }
        for text, expected in cases.items():
            self.assertEqual(strip_refs(text), expected, text)


if __name__ == "__main__":
    unittest.main()
