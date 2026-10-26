"""Local HTTP server that keeps Kokoro loaded and plays speech through the speakers.

Loading Kokoro takes a few seconds, so the Stop hook talks to this always-on
server instead of loading the model on every reply.

Endpoints (all on 127.0.0.1):
  GET  /health            -> {"ok": true, "speaking": bool, "voice": str}
  POST /speak             -> {"text": str, "voice"?: str, "speed"?: float,
                              "interrupt"?: bool, "wait"?: bool, "raw"?: bool}
                             raw=true skips markdown cleanup.
  POST /stop              -> cut playback and clear the queue
  POST /synthesize        -> same body as /speak; returns a WAV instead of playing
  POST /shutdown
"""
from __future__ import annotations

import io
import json
import logging
import os
import queue
import sys
import threading
import time
import urllib.error
import urllib.request
import warnings
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np

from . import config
from .clean import markdown_to_speech, split_sentences

SAMPLE_RATE = 24_000
BLOCK = 1024  # frames per write; small so /stop takes effect within ~40 ms

log = logging.getLogger("claudevoice.server")


_COREAUDIO = None


def default_output_id() -> int | None:
    """The system default output device's ID on macOS (microseconds to read).

    Returns None elsewhere, which makes the speaker reopen for every reply.
    """
    global _COREAUDIO
    if sys.platform != "darwin":
        return None
    try:
        import ctypes
        import ctypes.util

        if _COREAUDIO is None:
            _COREAUDIO = ctypes.CDLL(ctypes.util.find_library("CoreAudio"))

        class _Address(ctypes.Structure):
            _fields_ = [("selector", ctypes.c_uint32), ("scope", ctypes.c_uint32),
                        ("element", ctypes.c_uint32)]

        def fourcc(code: str) -> int:
            return int.from_bytes(code.encode(), "big")

        address = _Address(fourcc("dOut"), fourcc("glob"), 0)
        device = ctypes.c_uint32(0)
        size = ctypes.c_uint32(ctypes.sizeof(device))
        status = _COREAUDIO.AudioObjectGetPropertyData(
            1, ctypes.byref(address), 0, None, ctypes.byref(size), ctypes.byref(device))
        return int(device.value) if status == 0 else None
    except Exception:  # noqa: BLE001
        return None


class Speaker:
    """Owns the Kokoro pipeline, a synthesis queue, and the audio output stream."""

    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg
        self._queue: "queue.Queue[tuple[int, str, str, float]]" = queue.Queue()
        self._generation = 0  # bumped on /stop; stale queue items are dropped
        self._stop_playback = threading.Event()
        self._idle = threading.Event()
        self._idle.set()
        self._lock = threading.Lock()
        self.pipeline = None
        self.last_error = ""
        self.output_device = ""
        self._worker = threading.Thread(target=self._run, name="kokoro-worker", daemon=True)

    def start(self) -> None:
        t0 = time.time()
        warnings.filterwarnings("ignore")
        from kokoro import KPipeline  # heavy import; done once

        self.pipeline = KPipeline(lang_code=self.cfg["lang_code"], repo_id="hexgrad/Kokoro-82M")
        # Warm the voice and the graph so the first real reply has no cold start.
        for _ in self.pipeline("Ready.", voice=self.cfg["voice"], speed=1.0):
            pass
        log.info("Kokoro ready in %.1fs (voice=%s)", time.time() - t0, self.cfg["voice"])
        self._worker.start()

    # -- public API -----------------------------------------------------------
    @property
    def speaking(self) -> bool:
        return not self._idle.is_set()

    def speak(self, text: str, *, voice: str | None, speed: float | None, interrupt: bool,
              engine: str | None = None) -> int:
        if interrupt:
            self.stop()
        with self._lock:
            gen = self._generation
        # Settings are re-read per request so the voice bar's changes apply at once.
        self.cfg = config.load()
        engine = engine or self.cfg.get("tts_engine", "kokoro")
        if engine == "kokoro":
            voice = voice or self.cfg["voice"]
        else:
            voice = voice or self.cfg["tts_voice"]
        speed = float(speed or self.cfg["speed"])
        # Local synthesis streams sentence by sentence; a cloud engine takes the whole text at once.
        chunks = split_sentences(text) if engine == "kokoro" else ([text.strip()] if text.strip() else [])
        if not chunks:
            return 0
        log.info("speak %d chars as %s:%s", len(text), engine, voice)
        self._idle.clear()
        for chunk in chunks:
            self._queue.put((gen, chunk, f"{engine}:{voice}", speed))
        return len(chunks)

    def stop(self) -> None:
        with self._lock:
            self._generation += 1
        self._stop_playback.set()
        # Drain anything queued; items carry the old generation anyway.
        try:
            while True:
                self._queue.get_nowait()
        except queue.Empty:
            pass
        # The worker finishes its current ~40 ms block and then idles.
        self._idle.set()

    def wait(self, timeout: float | None = None) -> bool:
        return self._idle.wait(timeout)

    def synthesize(self, text: str, *, voice: str | None, speed: float | None) -> np.ndarray:
        voice = voice or self.cfg["voice"]
        speed = float(speed or self.cfg["speed"])
        pieces = [a for a in self._generate(text, f"kokoro:{voice}", speed)]
        return np.concatenate(pieces) if pieces else np.zeros(0, dtype=np.float32)

    # -- internals ------------------------------------------------------------
    def _generate(self, text: str, engine_voice: str, speed: float):
        """Yield float32 24 kHz mono audio for `text` from the engine named in `engine_voice`."""
        engine, _, voice = engine_voice.partition(":")
        if engine == "openai":
            yield self._cloud_openai(text, voice, speed)
        elif engine == "elevenlabs":
            yield self._cloud_elevenlabs(text, voice)
        else:
            for _, _, audio in self.pipeline(text, voice=voice, speed=speed):
                if audio is None:
                    continue
                yield audio.detach().cpu().numpy().astype(np.float32)

    def _key(self, env_name: str) -> str:
        key = os.environ.get(env_name, "") or _login_shell_var(env_name)
        if not key:
            raise RuntimeError(
                f"the {env_name} environment variable is not set (add `export {env_name}=...` "
                "to your ~/.zshrc, then restart the voice server with: claudevoice stop-server)"
            )
        return key

    def _cloud_openai(self, text: str, voice: str, speed: float) -> np.ndarray:
        """OpenAI-compatible POST /audio/speech, asking for raw 24 kHz 16-bit PCM."""
        cfg = self.cfg
        body = json.dumps({"model": cfg["tts_model"], "voice": voice, "input": text,
                           "response_format": "pcm", "speed": speed}).encode()
        req = urllib.request.Request(
            f"{cfg['tts_base_url'].rstrip('/')}/audio/speech", data=body, method="POST",
            headers={"Authorization": f"Bearer {self._key(cfg['tts_api_key_env'])}",
                     "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            pcm = resp.read()
        return np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0

    def _cloud_elevenlabs(self, text: str, voice_id: str) -> np.ndarray:
        cfg = self.cfg
        body = json.dumps({"text": text, "model_id": cfg["elevenlabs_model"]}).encode()
        req = urllib.request.Request(
            f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}?output_format=pcm_24000",
            data=body, method="POST",
            headers={"xi-api-key": self._key(cfg["elevenlabs_api_key_env"]), "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            pcm = resp.read()
        return np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0

    def _open_stream(self):
        """Open the speaker on the CURRENT system default output.

        PortAudio snapshots the device list when it starts, so a long-running
        server would keep playing into whatever was the default back then (laptop
        speakers after headphones were plugged in, a monitor that went away...).
        Re-initialising first makes the new stream follow the device the user hears now.
        """
        import sounddevice as sd

        try:
            sd._terminate()
            sd._initialize()
        except Exception:  # noqa: BLE001 - fall back to the existing device list
            log.exception("could not refresh the audio device list")
        stream = sd.OutputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32", blocksize=BLOCK)
        stream.start()
        try:
            name = sd.query_devices(stream.device)["name"]
        except Exception:  # noqa: BLE001
            name = str(stream.device)
        self.output_device = name
        log.info("playing on %s", name)
        return stream

    def _run(self) -> None:
        stream = None
        stream_output = None  # the system default output the stream was opened on
        was_idle = True
        while True:
            try:
                gen, chunk, voice, speed = self._queue.get(timeout=0.1)
            except queue.Empty:
                if self._queue.empty():
                    self._idle.set()
                    was_idle = True
                continue
            with self._lock:
                if gen != self._generation:
                    continue  # cancelled by /stop while waiting in the queue
                self._stop_playback.clear()
            if was_idle:
                # A new reply: make sure we play where the user is listening now.
                # Opening a Bluetooth stream costs ~0.6 s, so reuse it when the default is unchanged.
                was_idle = False
                current = default_output_id()
                if stream is not None and (current is None or current != stream_output):
                    try:
                        stream.stop()
                        stream.close()
                    except Exception:  # noqa: BLE001
                        pass
                    stream = None
                if stream is None:
                    try:
                        stream = self._open_stream()
                        stream_output = default_output_id()
                    except Exception as exc:  # noqa: BLE001 - keep the worker alive
                        self.last_error = f"could not open the speaker: {exc}"
                        log.exception(self.last_error)
                        continue
            try:
                for audio in self._generate(chunk, voice, speed):
                    if self._play(audio, stream):
                        break
                self.last_error = ""
            except urllib.error.HTTPError as exc:
                detail = exc.read()[:200].decode(errors="ignore")
                self.last_error = f"{voice.split(':')[0]} speech failed: HTTP {exc.code} {detail}"
                log.error(self.last_error)
                self._fallback(chunk, voice, speed, stream)
            except Exception as exc:  # noqa: BLE001 - keep the worker alive
                self.last_error = f"{voice.split(':')[0]} speech failed: {exc}"
                log.exception("synthesis failed for chunk: %r", chunk[:80])
                self._fallback(chunk, voice, speed, stream)
            # Tiny pause between sentences reads more naturally than butting them together.
            if not self._stop_playback.is_set():
                stream.write(np.zeros(int(SAMPLE_RATE * 0.12), dtype=np.float32))

    def _fallback(self, chunk: str, engine_voice: str, speed: float, stream) -> None:
        """A cloud engine failed: say why with Kokoro, then read the text with Kokoro."""
        engine = engine_voice.split(":")[0]
        if engine == "kokoro":
            return
        reason = "it needs an API key" if "environment variable" in self.last_error else "the request failed"
        notice = f"The {engine} voice is not available, {reason}. Using Kokoro instead."
        try:
            for text in (notice, chunk):
                for audio in self._generate(text, f"kokoro:{self.cfg['voice']}", speed):
                    if self._play(audio, stream):
                        return
        except Exception:  # noqa: BLE001
            log.exception("kokoro fallback failed")

    def _play(self, audio: np.ndarray, stream) -> bool:
        """Write audio in small blocks; returns True if interrupted."""
        for i in range(0, len(audio), BLOCK):
            if self._stop_playback.is_set():
                return True
            stream.write(np.ascontiguousarray(audio[i : i + BLOCK]))
        return False


_SHELL_VARS: dict[str, str] = {}


def _login_shell_var(name: str) -> str:
    """Read one variable from the user's login shell, for a server launched by an app.

    Only the named variable crosses, and only its presence is ever logged.
    """
    if name in _SHELL_VARS:
        return _SHELL_VARS[name]
    value = ""
    try:
        import subprocess

        out = subprocess.run(["/bin/zsh", "-lic", f"printenv {name}"], capture_output=True, text=True, timeout=8)
        value = out.stdout.strip().split("\n")[-1] if out.returncode == 0 else ""
    except Exception:  # noqa: BLE001
        value = ""
    _SHELL_VARS[name] = value
    log.info("%s %s in the login shell", name, "found" if value else "not found")
    return value


def wav_bytes(audio: np.ndarray) -> bytes:
    import soundfile as sf

    buf = io.BytesIO()
    sf.write(buf, audio, SAMPLE_RATE, format="WAV", subtype="PCM_16")
    return buf.getvalue()


def make_handler(speaker: Speaker, cfg: dict):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # quieter than the default stderr spam
            log.debug(fmt, *args)

        def _json(self, code: int, body: dict) -> None:
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b""
            try:
                return json.loads(raw or b"{}")
            except json.JSONDecodeError:
                return {}

        def do_GET(self):
            if self.path == "/health":
                live = config.load()
                self._json(200, {"ok": True, "speaking": speaker.speaking, "voice": live["voice"],
                                 "engine": live.get("tts_engine", "kokoro"), "last_error": speaker.last_error,
                                 "output_device": speaker.output_device})
            else:
                self._json(404, {"error": "not found"})

        def do_POST(self):
            body = self._body()
            if self.path == "/speak":
                text = body.get("text", "")
                if not body.get("raw"):
                    text = markdown_to_speech(text, max_chars=body.get("max_chars"))
                n = speaker.speak(
                    text,
                    voice=body.get("voice"),
                    speed=body.get("speed"),
                    interrupt=bool(body.get("interrupt", True)),
                    engine=body.get("engine"),
                )
                if body.get("wait"):
                    speaker.wait()
                self._json(200, {"ok": True, "chunks": n, "text": text})
            elif self.path == "/stop":
                speaker.stop()
                self._json(200, {"ok": True})
            elif self.path == "/synthesize":
                text = body.get("text", "")
                if not body.get("raw"):
                    text = markdown_to_speech(text)
                audio = speaker.synthesize(text, voice=body.get("voice"), speed=body.get("speed"))
                data = wav_bytes(audio)
                self.send_response(200)
                self.send_header("Content-Type", "audio/wav")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            elif self.path == "/shutdown":
                self._json(200, {"ok": True})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
            else:
                self._json(404, {"error": "not found"})

    return Handler


def main(argv: list[str] | None = None) -> int:
    cfg = config.load()
    config.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.DEBUG if os.environ.get("CLAUDEVOICE_DEBUG") else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        handlers=[logging.StreamHandler(sys.stderr), logging.FileHandler(config.SERVER_LOG_PATH)],
    )
    speaker = Speaker(cfg)
    speaker.start()
    server = ThreadingHTTPServer(("127.0.0.1", int(cfg["port"])), make_handler(speaker, cfg))
    log.info("claudevoice server listening on %s", config.server_url(cfg))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        speaker.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
