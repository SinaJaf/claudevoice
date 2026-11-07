"""The setup report: what it shows, and what it keeps out. No model or sound hardware needed.

Run: .venv/bin/python -m unittest tests.test_report
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("CLAUDEVOICE_HOME", tempfile.mkdtemp())  # never the person's own settings

from claudevoice import config, report  # noqa: E402


class Redaction(unittest.TestCase):
    def test_paths_never_show(self) -> None:
        # A name can hold spaces, so everything after a path is hidden too, up to a quote.
        for path in ("/Users/sam/notes/plan.md", "C:\\Users\\Sam\\My Documents\\plan.txt", "~/coding/secret-project/a.py",
                     "/home/sam/.claudevoice/server.log", "/Users/sam/My diary.md"):
            self.assertEqual(report.redact(f"could not read {path} now"), "could not read <path>", path)
            self.assertEqual(report.redact(f"could not read '{path}' now"), "could not read '<path>' now", path)

    def test_ordinary_slashes_stay(self) -> None:
        for text in ("speak and/or listen", "speed 3/4 today", "open http://127.0.0.1:8765/health"):
            self.assertEqual(report.redact(text), text)

    def test_names_in_device_names_never_show(self) -> None:
        self.assertEqual(report.redact("Sam's AirPods Pro"), "<name>'s AirPods Pro")
        self.assertEqual(report.redact("Sina’s MacBook Pro Microphone"), "<name>’s MacBook Pro Microphone")
        self.assertEqual(report.redact("MacBook Pro Speakers"), "MacBook Pro Speakers")


class Report(unittest.TestCase):
    def test_it_runs_with_no_server_and_says_so(self) -> None:
        with mock.patch.object(config, "server_url", lambda cfg=None: "http://127.0.0.1:9"):
            text = report.build()
        for heading in ("claudevoice setup report", "**Packages**", "**Settings**", "**Voice files**", "**Sound**", "**Voice server**"):
            self.assertIn(heading, text)
        self.assertIn("Voice server: not running", text)

    def test_logs_give_errors_once_and_leave_out_noise_and_text(self) -> None:
        log = Path(config.SERVER_LOG_PATH)
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text("\n".join([
            "2026-11-07 04:58:43,375 claudevoice.server INFO read My diary.md: 300 chars in 3 pieces",
            "2026-11-07 04:58:43,375 huggingface_hub WARNING You are sending unauthenticated requests to the HF Hub.",
            "2026-11-07 04:58:44,000 claudevoice.server ERROR could not read /Users/sam/My diary.md",
            "2026-11-07 04:58:45,000 claudevoice.server ERROR could not read /Users/sam/My diary.md",
            "Traceback (most recent call last):",
            "RuntimeError: the graphics card is not available",
        ]) + "\n", encoding="utf-8")
        found = report._problems()
        self.assertEqual(len(found), 2, found)
        self.assertIn("could not read <path>", found[0])
        self.assertIn("RuntimeError: the graphics card is not available", found[1])
        joined = "\n".join(found)
        self.assertNotIn("diary", joined)
        self.assertNotIn("HF Hub", joined)

    def test_a_chosen_microphone_is_not_named(self) -> None:
        with mock.patch.object(config, "load", lambda: {**config.DEFAULTS, "input_device": "Sam's USB Mic"}), \
             mock.patch.object(config, "server_url", lambda cfg=None: "http://127.0.0.1:9"):
            text = report.build()
        self.assertIn("input_device: chosen", text)
        self.assertNotIn("Sam", text)


if __name__ == "__main__":
    unittest.main()
