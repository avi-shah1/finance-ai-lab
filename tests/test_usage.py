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


if __name__ == "__main__":
    unittest.main()
