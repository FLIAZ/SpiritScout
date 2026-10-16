"""浏览器失败按实际日志解释，不能把引用存在等同于可点击。"""

from __future__ import annotations

import unittest

from spiritscout.tools.diagnostics import browser_failure, clean_tool_text


class BrowserDiagnosticTests(unittest.TestCase):
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
