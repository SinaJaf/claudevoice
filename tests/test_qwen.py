"""Which computers are offered Qwen3-TTS, and the NVIDIA engine's use of its package. No model or GPU.

Run: .venv/bin/python -m unittest tests.test_qwen
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

os.environ.setdefault("CLAUDEVOICE_HOME", tempfile.mkdtemp())  # never the person's own settings

import numpy as np  # noqa: E402

from claudevoice import qwen  # noqa: E402


def smi(stdout: str = "", code: int = 0, error: Exception | None = None):
    def run(*args, **kwargs):
        if error:
            raise error
        return subprocess.CompletedProcess(args, code, stdout, "")
    return mock.patch.object(qwen.subprocess, "run", run)


def on(system: str, machine: str):
    return [mock.patch.object(qwen.sys, "platform", system), mock.patch.object(qwen.platform, "machine", lambda: machine)]


class Check(unittest.TestCase):
    def computer(self, system: str = "win32", machine: str = "AMD64", **nvidia) -> dict:
        patches = on(system, machine) + [smi(**nvidia)]
        for p in patches:
            p.start()
        try:
            return qwen.check()
        finally:
            for p in patches:
                p.stop()

    def test_apple_silicon_can(self) -> None:
        self.assertTrue(self.computer("darwin", "arm64")["can"])

    def test_the_lighter_engine_needs_macos_14(self) -> None:
        for version, expected in (("27.0.1", True), ("14.0", True), ("13.6", False), ("", False)):
            with mock.patch.object(qwen.platform, "mac_ver", lambda v=version: (v, ("", "", ""), "")):
                found = self.computer("darwin", "arm64")
            self.assertEqual(found["light"], expected, version)
            self.assertTrue(found["can"], "the standard engine does not depend on it")

    def test_an_intel_mac_cannot(self) -> None:
        self.assertFalse(self.computer("darwin", "x86_64")["can"])

    def test_a_recent_nvidia_card_can(self) -> None:
        found = self.computer(stdout="NVIDIA GeForce RTX 4060 Laptop GPU, 8188, 8.9, 561.09\n")
        self.assertEqual(found, {"can": True, "why": "", "gpu": "NVIDIA GeForce RTX 4060 Laptop GPU", "light": False})
        self.assertTrue(self.computer("linux", "x86_64", stdout="NVIDIA GeForce RTX 3060, 12288, 8.6, 550.54\n")["can"])

    def test_a_card_too_old(self) -> None:
        found = self.computer(stdout="NVIDIA GeForce GTX 1060, 6144, 6.1, 560.10\n")
        self.assertFalse(found["can"])
        self.assertIn("GTX 1060 is too old", found["why"])

    def test_too_little_memory(self) -> None:
        found = self.computer(stdout="NVIDIA GeForce GTX 1650, 2048, 7.5, 560.10\n")
        self.assertFalse(found["can"])
        self.assertIn("too little memory", found["why"])

    def test_a_4_gb_card_can(self) -> None:
        self.assertTrue(self.computer(stdout="NVIDIA GeForce GTX 1650, 4096, 7.5, 560.10\n")["can"])

    def test_an_old_driver(self) -> None:
        found = self.computer(stdout="NVIDIA GeForce RTX 2060, 6144, 7.5, 512.15\n")
        self.assertFalse(found["can"])
        self.assertIn("newer NVIDIA driver", found["why"])

    def test_the_best_of_two_cards(self) -> None:
        found = self.computer(stdout="NVIDIA GeForce GT 730, 2048, 3.5, 560.10\nNVIDIA GeForce RTX 3070, 8192, 8.6, 560.10\n")
        self.assertEqual(found["gpu"], "NVIDIA GeForce RTX 3070")
        self.assertTrue(found["can"])

    def test_no_nvidia(self) -> None:
        self.assertFalse(self.computer(error=FileNotFoundError())["can"])
        self.assertFalse(self.computer(code=9)["can"])
        self.assertFalse(self.computer(stdout="something else entirely\n")["can"])
        self.assertFalse(self.computer(error=subprocess.TimeoutExpired("nvidia-smi", 15))["can"])

    def test_arm_windows_cannot(self) -> None:
        self.assertFalse(self.computer(machine="ARM64", stdout="x, 8192, 8.6, 560.10\n")["can"])


class NativeVoice(unittest.TestCase):
    def setUp(self) -> None:
        self.calls: dict = {}
        calls = self.calls

        class QwenTTS:
            @classmethod
            def from_pretrained(cls, model, **options):
                calls["model"], calls["options"] = model, options
                return cls()

            def stream(self, **options):
                calls["stream"] = options
                yield np.ones(100, dtype=np.float64) * 0.5, 24000
                yield np.ones(100, dtype=np.float32) * 0.5, 48000

            def set_log_callback(self, callback):
                calls["log"] = callback

            def close(self):
                calls["closed"] = True

        package = types.ModuleType("qwentts_cpp")
        package.QwenTTS = QwenTTS
        package.resolve_gguf_paths = lambda model, quant: calls.update(fetched=(model, quant))
        for p in [mock.patch.dict(sys.modules, {"qwentts_cpp": package}), *on("win32", "AMD64")]:
            p.start()
            self.addCleanup(p.stop)

    def test_it_loads_the_small_model_with_short_chunks(self) -> None:
        qwen.load()
        self.assertEqual(self.calls["model"], "Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice")
        self.assertEqual(self.calls["options"], {"quant": "Q8_0", "codec_chunk_sec": 0.64})

    def test_it_asks_for_the_voice_by_its_lowercase_name(self) -> None:
        list(qwen.load().stream("Hello there.", "Aiden"))
        self.assertEqual(self.calls["stream"], {"text": "Hello there.", "speaker": "aiden", "lang": "english"})

    def test_audio_comes_out_as_float32_at_the_speakers_rate(self) -> None:
        first, second = list(qwen.load().stream("Hello there.", "Aiden"))
        self.assertEqual(first.dtype, np.float32)
        self.assertEqual(len(first), 100)
        self.assertEqual(second.dtype, np.float32)
        self.assertEqual(len(second), 50, "48 kHz halved")

    def test_only_the_engines_warnings_reach_the_log(self) -> None:
        qwen.load()
        with self.assertLogs("claudevoice.server", "WARNING") as logs:
            self.calls["log"](0, "[Sample] step=0")
            self.calls["log"](1, "[Prompt] Built: 13 ids")
            self.calls["log"](2, "out of graphics memory\n")
        self.assertEqual(logs.output, ["WARNING:claudevoice.server:qwentts: out of graphics memory"])

    def test_closing_it_frees_the_model(self) -> None:
        qwen.load().close()
        self.assertTrue(self.calls["closed"])

    def test_fetching_takes_only_the_files_this_engine_uses(self) -> None:
        qwen.fetch()
        self.assertEqual(self.calls["fetched"], ("Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice", "Q8_0"))


class EngineChoice(unittest.TestCase):
    def setUp(self) -> None:
        self.calls: list[str] = []
        calls = self.calls

        class QwenTTS:
            @classmethod
            def from_pretrained(cls, model, **options):
                calls.append("native loaded")
                return cls()

            def set_log_callback(self, callback):
                pass

        package = types.ModuleType("qwentts_cpp")
        package.QwenTTS = QwenTTS
        package.resolve_gguf_paths = lambda model, quant: calls.append("native fetched")
        for p in [mock.patch.dict(sys.modules, {"qwentts_cpp": package}), *on("darwin", "arm64"),
                  mock.patch.object(qwen, "_MlxVoice", lambda: calls.append("mlx loaded"))]:
            p.start()
            self.addCleanup(p.stop)

    def test_a_mac_loads_the_engine_chosen(self) -> None:
        qwen.load(qwen.STANDARD)
        self.assertEqual(self.calls, ["mlx loaded"])
        self.calls.clear()
        qwen.load(qwen.LIGHT)
        self.assertEqual(self.calls, ["native loaded"])

    def test_the_lighter_engine_fetches_its_own_files(self) -> None:
        qwen.fetch(qwen.LIGHT)
        self.assertEqual(self.calls, ["native fetched"])


class Speakers(unittest.TestCase):
    def test_known_voices_and_a_stranger(self) -> None:
        self.assertEqual(qwen.speaker_for("ryan"), "Ryan")
        self.assertEqual(qwen.speaker_for("Aiden"), "Aiden")
        self.assertEqual(qwen.speaker_for("alloy"), "Aiden")


if __name__ == "__main__":
    unittest.main()
