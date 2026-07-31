#!/usr/bin/env python3
import os
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.refresh_official_models import (  # noqa: E402
    discover_deepseek_stable_models,
    discover_openai_latest_models,
    text_from_html,
)
from src import main  # noqa: E402


class OfficialModelDiscoveryTests(unittest.TestCase):
    def test_future_openai_family_is_discovered_from_latest_guide(self) -> None:
        guide = """
        ---
        latestModelInfo:
          model: gpt-5.7-sol
        ---
        Use `gpt-5.7`, `gpt-5.7-sol`, `gpt-5.7-terra`, or `gpt-5.7-luna`.
        """
        self.assertEqual(
            discover_openai_latest_models(guide),
            (
                "gpt-5.7-sol",
                "gpt-5.7",
                ["gpt-5.7-sol", "gpt-5.7-terra", "gpt-5.7-luna"],
            ),
        )

    def test_future_deepseek_stable_models_are_discovered(self) -> None:
        pricing = """
        Stable: deepseek-v4-pro deepseek-v4-flash
        New stable: deepseek-v5-pro deepseek-v5-flash
        """
        self.assertEqual(
            discover_deepseek_stable_models(pricing),
            ("deepseek-v5-pro", "deepseek-v5-flash"),
        )

    def test_html_tool_artifacts_are_removed_before_parsing(self) -> None:
        raw = "<div>1,050,000<!-- --> context</div><!--astro:end--><span>&amp; ok</span>"
        self.assertEqual(text_from_html(raw), "1,050,000 context & ok")


class UnifiedProxyRoutingTests(unittest.TestCase):
    def test_catalog_alias_resolves_to_concrete_gpt_variant(self) -> None:
        self.assertEqual(
            main._normalize_gpt_model_for_chatgpt_backend("gpt-5.6"),
            "gpt-5.6-sol",
        )

    def test_future_explicit_deepseek_slug_is_not_rewritten_to_v4(self) -> None:
        self.assertEqual(
            main._resolve_deepseek_model("deepseek-v5-pro"),
            ("deepseek-v5-pro", None),
        )

    def test_deepseek_default_thinking_and_effort(self) -> None:
        request = {
            "model": "deepseek-v4-pro",
            "stream": False,
            "input": [
                {
                    "role": "user",
                    "content": [{"type": "input_text", "text": "hi"}],
                }
            ],
            "temperature": 0.2,
        }
        chat_body = main.build_chat_body(request)["chat_body"]
        self.assertEqual(chat_body["thinking"], {"type": "enabled"})
        self.assertEqual(chat_body["reasoning_effort"], "high")
        self.assertNotIn("temperature", chat_body)

    def test_luna_404_explains_platform_api_key_requirement(self) -> None:
        if main.OPENAI_API_KEY:
            self.skipTest("Platform key is configured, so this fallback message is not used")
        message = main._gpt_chatgpt_error_message(
            "gpt-5.6-luna",
            404,
            '{"error":{"message":"Model not found gpt-5.6-luna"}}',
        )
        self.assertIn("openai_api_key", message)


if __name__ == "__main__":
    unittest.main()
