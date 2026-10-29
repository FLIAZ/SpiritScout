"""浏览器失败按实际日志解释，不能把引用存在等同于可点击。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from spiritscout.tools.diagnostics import (
    browser_failure,
    clean_tool_text,
    compact_mcp_text,
)


class BrowserDiagnosticTests(unittest.TestCase):
    def test_long_failure_keeps_late_diagnostic_and_full_evidence(self) -> None:
        text = "launch flags " * 2000 + "\nFATAL Socket path too long: /a/b/SingletonSocket"
        with tempfile.TemporaryDirectory() as temp:
            summary, metadata = compact_mcp_text("browser_tabs", text, failed=True, output_dir=Path(temp))
            self.assertLess(len(summary), 500)
            self.assertEqual(metadata["failure_kind"], "browser_temp_path_too_long")
            self.assertEqual(Path(metadata["raw_output_path"]).read_text(), text)

    def test_large_success_retains_both_ends_and_retrievable_original(self) -> None:
        text = "start\n" + "data " * 5000 + "\nimportant tail"
        with tempfile.TemporaryDirectory() as temp:
            summary, metadata = compact_mcp_text("custom_tool", text, failed=False, output_dir=Path(temp))
            self.assertIn("start", summary)
            self.assertIn("important tail", summary)
            self.assertLess(len(summary), 5500)
            self.assertEqual(Path(metadata["raw_output_path"]).read_text(), text)

    def test_small_results_are_not_rewritten_or_stored(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            summary, metadata = compact_mcp_text("browser_snapshot", "ref=e17 button 登录", failed=False,
                                                output_dir=Path(temp) / "unused")
            self.assertEqual(summary, "ref=e17 button 登录")
            self.assertEqual(metadata, {})
            self.assertFalse((Path(temp) / "unused").exists())

    def test_missing_cdp_is_distinct_from_target_site_failure(self) -> None:
        result = browser_failure("browser_navigate", "async initializeServer: "
            "connect ECONNREFUSED 127.0.0.1:9223\nretrieving websocket url")
        self.assertEqual(result["failure_kind"], "browser_connection_unavailable")
        self.assertIn("尚未访问题目页面", result["failure_summary"])

    def test_viewport_reason_is_preserved_beyond_ui_preview_length(self) -> None:
        text = "Timeout 5000ms exceeded\n" + "waiting for locator\n" * 40
        text += "element is outside of the viewport"
        result = browser_failure("browser_click", text)
        self.assertEqual(result["failure_kind"], "target_outside_viewport")
        self.assertIn("可视区域外", result["failure_summary"])

    def test_generic_timeout_does_not_invent_target_geometry(self) -> None:
        result = browser_failure("browser_click", "TimeoutError: Timeout 5000ms exceeded")
        self.assertEqual(result["failure_kind"], "browser_timeout")
        self.assertNotIn("视口", result["failure_summary"])

    def test_last_observed_blocker_takes_precedence(self) -> None:
        result = browser_failure("browser_click", "element is outside of the viewport\noverlay intercepts pointer events")
        self.assertEqual(result["failure_kind"], "target_obscured")

    def test_strip_terminal_codes_without_losing_locator_or_reason(self) -> None:
        text = clean_tool_text("\x1b[2mwaiting for locator('aria-ref=e247')\x1b[22m")
        self.assertEqual(text, "waiting for locator('aria-ref=e247')")

    def test_non_browser_error_is_not_misclassified(self) -> None:
        self.assertEqual(browser_failure("compile", "Timeout 5000ms exceeded"), {})
