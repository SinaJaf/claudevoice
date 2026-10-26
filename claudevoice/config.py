"""User configuration for claudevoice, read from ~/.claudevoice/config.json.

Stdlib-only so the Stop hook can import it with the system python.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

CONFIG_DIR = Path(os.environ.get("CLAUDEVOICE_HOME", Path.home() / ".claudevoice"))
CONFIG_PATH = CONFIG_DIR / "config.json"
ENABLED_FLAG = CONFIG_DIR / "enabled"
LOG_PATH = CONFIG_DIR / "claudevoice.log"
SERVER_LOG_PATH = CONFIG_DIR / "server.log"
# The voice panel touches this file to cut a recording short.
LISTEN_STOP_FLAG = CONFIG_DIR / "listen.stop"
# ...and this one to finish a hold-mode recording and transcribe it.
LISTEN_DONE_FLAG = CONFIG_DIR / "listen.done"
LISTEN_MUTE_FLAG = CONFIG_DIR / "listen.mute"  # "1" while the mic is muted
LISTEN_LOCK = CONFIG_DIR / "listen.lock"  # only one microphone recording at a time

DEFAULTS: dict = {
    # Kokoro voice. Blend voices by listing several, e.g. "af_heart,af_bella".
    # See https://huggingface.co/hexgrad/Kokoro-82M/blob/main/VOICES.md
    "voice": "af_heart",
    # 'a' = American English, 'b' = British English.
    "lang_code": "a",
    # 1.0 is Kokoro's native pace; 1.05-1.15 sounds more conversational.
    "speed": 1.05,
    "port": 8765,
    # Longest reply (after markdown cleanup) that the Stop hook will read aloud.
    "max_chars": 2500,
    # Listening (speech-to-text). "local" runs mlx-whisper on the Mac; "openai" posts the
    # audio to an OpenAI-compatible /audio/transcriptions endpoint (OpenAI, Groq, ...).
    "stt_engine": "local",
    # Short name: small, medium, large-v3-turbo, large-v3 (mapped to the backend's repo).
    "whisper_model": "large-v3-turbo",
    "stt_model": "gpt-4o-mini-transcribe",
    "stt_base_url": "https://api.openai.com/v1",
    "stt_api_key_env": "OPENAI_API_KEY",
    # Speaking (text-to-speech). "kokoro" is local; "openai" and "elevenlabs" are cloud.
    # Cloud keys are read from the environment variable named here, never stored in this file.
    "tts_engine": "kokoro",
    "tts_voice": "nova",
    "tts_model": "gpt-4o-mini-tts",
    "tts_base_url": "https://api.openai.com/v1",
    "tts_api_key_env": "OPENAI_API_KEY",
    "elevenlabs_model": "eleven_turbo_v2_5",
    "elevenlabs_api_key_env": "ELEVENLABS_API_KEY",
    # Seconds of silence after speech before a recording auto-stops.
    "silence_seconds": 1.2,
    # Energy threshold (RMS, 0-1) that counts as speech. Raise in noisy rooms.
    "vad_threshold": 0.012,
    # Longest single recording, in seconds.
    "max_record_seconds": 90,
    # Microphone by name (as `claudevoice devices` lists them); empty = the system default.
    "input_device": "",
}


def load() -> dict:
    cfg = dict(DEFAULTS)
    try:
        cfg.update(json.loads(CONFIG_PATH.read_text()))
    except FileNotFoundError:
        pass
    except json.JSONDecodeError as exc:
        print(f"claudevoice: ignoring malformed {CONFIG_PATH}: {exc}")
    for key in ("voice", "lang_code", "port", "speed", "whisper_model", "stt_engine", "tts_engine"):
        env = os.environ.get(f"CLAUDEVOICE_{key.upper()}")
        if env:
            cfg[key] = type(DEFAULTS[key])(env)
    return cfg


def save(cfg: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2) + "\n")


def is_enabled() -> bool:
    return ENABLED_FLAG.exists()


def set_enabled(value: bool) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if value:
        ENABLED_FLAG.touch()
    else:
        ENABLED_FLAG.unlink(missing_ok=True)


def server_url(cfg: dict | None = None) -> str:
    cfg = cfg or load()
    return f"http://127.0.0.1:{cfg['port']}"
