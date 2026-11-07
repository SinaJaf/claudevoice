"""`claudevoice report`: a setup report to paste into a bug report.

It reads this computer and prints Markdown; nothing is sent anywhere. It holds no text that was
read aloud, no transcripts and no file names: paths show as <path>, the home folder as ~, and a
person's name in a device name ("Sam's AirPods") as <name>'s.
"""
from __future__ import annotations

import json
import platform
import re
import sys
import urllib.request
from importlib import metadata
from pathlib import Path

from . import config

ROOT = Path(__file__).resolve().parent.parent

PACKAGES = ("kokoro", "torch", "numpy", "onnx-asr", "onnxruntime", "sounddevice", "spacy",
            "pypdfium2", "mlx-audio", "qwentts-cpp-python", "realtimetts-qwen-native",
            "faster-whisper", "mlx-whisper")
MODELS = {
    "Kokoro": "hexgrad/Kokoro-82M",
    "Parakeet v2": "istupakov/parakeet-tdt-0.6b-v2-onnx",
    "Parakeet v3": "istupakov/parakeet-tdt-0.6b-v3-onnx",
    "Smart Turn": "pipecat-ai/smart-turn-v3",
    "Qwen3-TTS (Mac)": "mlx-community/Qwen3-TTS-12Hz-0.6B-CustomVoice-8bit",
    "Qwen3-TTS (qwentts.cpp)": "Serveurperso/Qwen3-TTS-GGUF",
}
SETTINGS = ("tts_engine", "voice", "tts_voice", "speed", "lang_code", "stt_engine", "parakeet_model", "port")
PROBLEM = re.compile(r"\b(ERROR|WARNING|CRITICAL)\b|\w*(Error|Exception):")
# Common and harmless: left out so the lines that matter show.
HARMLESS = ("unauthenticated requests to the HF Hub", "words count mismatch")
_STAMP = re.compile(r"^\d{4}-\d\d-\d\d [\d:,]+ ")

# A path, from where it starts to the next quote or the end of the line: names of folders and
# files can hold spaces, so there is no telling where one ends sooner.
_PATH = re.compile(r"(?<![\w:/])(?:[A-Za-z]:[\\/]|~[\\/]|/(?=[^\s/]+[\\/]))[^\n'\"<>|]*")
_POSSESSIVE = re.compile(r"\b[\w.-]+(['’])s\b")


def redact(text: str) -> str:
    """No paths and no people's names."""
    return _POSSESSIVE.sub(r"<name>\1s", _PATH.sub("<path>", text))


def _version() -> str:
    try:
        return json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text())["version"]
    except (OSError, ValueError, KeyError):
        return "unknown"


def _package(name: str) -> str:
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return "not installed"


def _models() -> list[str]:
    try:
        from huggingface_hub import constants

        hub = Path(constants.HF_HUB_CACHE)
    except Exception:  # noqa: BLE001
        hub = Path.home() / ".cache" / "huggingface" / "hub"
    rows = []
    for label, repo in MODELS.items():
        folder = hub / ("models--" + repo.replace("/", "--"))
        rows.append(f"- {label}: {'downloaded' if (folder / 'snapshots').is_dir() else 'not downloaded'}")
    return rows


def _audio() -> list[str]:
    try:
        import sounddevice as sd
    except Exception as exc:  # noqa: BLE001 - PortAudio missing is the usual reason
        hint = " (on Linux: sudo apt install libportaudio2)" if sys.platform.startswith("linux") else ""
        return [f"- Sound library: failed to load: {redact(str(exc))}{hint}"]
    rows = [f"- Sound library: {sd.get_portaudio_version()[1]}"]
    try:
        devices = sd.query_devices()
        ins = [d for d in devices if d["max_input_channels"] > 0]
        outs = [d for d in devices if d["max_output_channels"] > 0]
        rows.append(f"- Devices: {len(ins)} for input, {len(outs)} for output")
        for kind, index in (("input", sd.default.device[0]), ("output", sd.default.device[1])):
            name = devices[index]["name"] if isinstance(index, int) and 0 <= index < len(devices) else "none"
            rows.append(f"- Default {kind}: {redact(name)}")
    except Exception as exc:  # noqa: BLE001
        rows.append(f"- Devices: could not be listed: {redact(str(exc))}")
    return rows


def _server(cfg: dict) -> list[str]:
    try:
        with urllib.request.urlopen(f"{config.server_url(cfg)}/health", timeout=2) as r:
            health = json.loads(r.read())
    except Exception:  # noqa: BLE001
        return ["- Voice server: not running (it starts the first time something is spoken)"]
    rows = [f"- Voice server: running, engine {health.get('engine')}, voice {health.get('voice')}"]
    if health.get("last_error"):
        rows.append(f"- Last error: {redact(str(health['last_error']))}")
    if health.get("output_device"):
        rows.append(f"- Playing on: {redact(str(health['output_device']))}")
    return rows


def _problems(limit: int = 12) -> list[str]:
    """The last warnings and errors in the logs, each once, harmless ones left out."""
    found: list[str] = []
    seen: set[str] = set()
    for log in (config.SERVER_LOG_PATH, config.CONFIG_DIR / "listen.log"):
        try:
            lines = log.read_text(encoding="utf-8", errors="replace").splitlines()[-400:]
        except OSError:
            continue
        for line in lines:
            if not PROBLEM.search(line) or any(h in line for h in HARMLESS):
                continue
            text = redact(line.strip())[:240]
            key = _STAMP.sub("", text)
            if key in seen:
                continue
            seen.add(key)
            found.append(f"{log.name}: {text}")
    return found[-limit:]


def build() -> str:
    cfg = config.load()
    from . import qwen

    qwen_found = qwen.check()
    parts = [
        "### claudevoice setup report",
        "",
        f"- claudevoice {_version()}, Python {platform.python_version()}",
        f"- System: {platform.system()} {platform.release()} ({platform.machine()}), {redact(platform.platform())}",
        "",
        "**Packages**",
        *[f"- {name}: {_package(name)}" for name in PACKAGES],
        "",
        "**Settings**",
        *[f"- {key}: {cfg.get(key)}" for key in SETTINGS],
        f"- input_device: {'chosen' if cfg.get('input_device') else 'system default'}",
        "",
        "**Voice files**",
        *_models(),
        f"- Qwen3-TTS here: {'yes' if qwen_found['can'] else 'no, ' + qwen_found['why']}"
        + (", lighter engine too" if qwen_found.get("light") else ""),
        "",
        "**Sound**",
        *_audio(),
        "",
        "**Voice server**",
        *_server(cfg),
    ]
    problems = _problems()
    parts += ["", "**Recent problems in the logs**"] + ([f"    {p}" for p in problems] or ["- none"])
    return "\n".join(parts) + "\n"


def main(argv: list[str]) -> int:
    print(build())
    return 0
