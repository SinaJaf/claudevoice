"""Local HTTP server that keeps Kokoro loaded and plays speech through the speakers.

Loading Kokoro takes a few seconds, so the Stop hook talks to this always-on
server instead of loading the model on every reply.

Endpoints (all on 127.0.0.1):
  GET  /health            -> {"ok": true, "speaking": bool, "voice": str}
  POST /speak             -> {"text": str, "voice"?: str, "speed"?: float,
                              "interrupt"?: bool, "wait"?: bool, "raw"?: bool}
                             raw=true skips markdown cleanup.
  POST /stop              -> cut playback and clear the queue (and forget any reading)
  POST /read              -> {"path": str, "start"?: int, "end"?: int} or {"text": str, "title"?: str},
                             plus "voice"/"speed"/"engine"; or {"resume": true}. Reads a file
                             (markdown, text, PDF) or selected text sentence by sentence,
                             keeping its place: /health reports "reading" {title, at, of, paused}.
                             start/end are lines, or pages for a PDF.
  POST /pause             -> stop reading, keeping the place for {"resume": true}
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
from pathlib import Path

import numpy as np

from . import config, devices
from .clean import markdown_to_speech, split_sentences
from .document import DocumentError, group, load, prepare_text, split_long

SAMPLE_RATE = 24_000
BLOCK = 1024  # frames per write; small so /stop takes effect within ~40 ms

log = logging.getLogger("claudevoice.server")

# The plugin folder this server runs from: the plugin replaces a server an older version
# of itself started (each version is installed in a folder of its own).
ROOT = str(Path(__file__).resolve().parent.parent)

CLOUD_PIECE = 1200  # characters per request when a cloud voice reads a document


class Reading:
    """A document being read: its pieces, and the one playing (or to resume from)."""

    def __init__(self, title: str, pieces: list[str], engine_voice: str, speed: float) -> None:
        self.title, self.pieces, self.engine_voice, self.speed = title, pieces, engine_voice, speed
        self.at = 0
        self.paused = False

    def state(self) -> dict:
        return {"title": self.title, "at": self.at, "of": len(self.pieces), "paused": self.paused}


class Speaker:
    """Owns the Kokoro pipeline, a synthesis queue, and the audio output stream."""

    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg
        # (generation, text, "engine:voice", speed, index into the reading or -1)
        self._queue: "queue.Queue[tuple[int, str, str, float, int]]" = queue.Queue()
        self.reading: Reading | None = None
        self._generation = 0  # bumped on /stop; stale queue items are dropped
        self._stop_playback = threading.Event()
        self._idle = threading.Event()
        self._idle.set()
        self._lock = threading.Lock()
        self.pipeline = None
        self.last_error = ""
        self.output_device = ""
        self._stream = None
        self._stream_output: int | None = None
        self._last_device_check = 0.0
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
            self._halt()  # a reading cut off this way can be resumed
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
            self._queue.put((gen, chunk, f"{engine}:{voice}", speed, -1))
        return len(chunks)

    def read(self, title: str, text: str, *, voice: str | None, speed: float | None,
             engine: str | None = None) -> int:
        """Read cleaned document text aloud from the start, replacing anything playing."""
        self.stop()
        self.cfg = config.load()
        engine = engine or self.cfg.get("tts_engine", "kokoro")
        voice = voice or (self.cfg["voice"] if engine == "kokoro" else self.cfg["tts_voice"])
        speed = float(speed or self.cfg["speed"])
        pieces = split_long(split_sentences(text))
        if engine != "kokoro":
            pieces = group(pieces, CLOUD_PIECE)
        if not pieces:
            return 0
        log.info("read %s: %d chars in %d pieces as %s:%s", title, len(text), len(pieces), engine, voice)
        self.reading = Reading(title, pieces, f"{engine}:{voice}", speed)
        self._queue_reading()
        return len(pieces)

    def pause(self) -> None:
        self._halt()

    def resume(self) -> bool:
        if self.reading is None:
            return False
        self._halt()
        self._queue_reading()
        return True

    def _queue_reading(self) -> None:
        reading = self.reading
        if reading is None:
            return
        reading.paused = False
        with self._lock:
            gen = self._generation
        self._idle.clear()
        for i in range(reading.at, len(reading.pieces)):
            self._queue.put((gen, reading.pieces[i], reading.engine_voice, reading.speed, i))

    def stop(self) -> None:
        """Cut playback and forget any reading."""
        self._halt()
        self.reading = None

    def _halt(self) -> None:
        """Cut playback; a reading keeps its place, paused."""
        if self.reading is not None:
            self.reading.paused = True
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

    # -- the speaker device ----------------------------------------------------
    # The stream follows the system default output: it reopens when the default
    # changes (checked before each sentence and about twice a second while
    # playing), and when a write fails because the device went away.
    # Opening a Bluetooth stream costs ~0.6 s, so an unchanged default is reused.

    def _open_stream(self) -> None:
        import sounddevice as sd

        self._close_stream()
        devices.refresh()  # see newly plugged / newly default devices
        stream = sd.OutputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32", blocksize=BLOCK)
        stream.start()
        try:
            name = sd.query_devices(stream.device)["name"]
        except Exception:  # noqa: BLE001
            name = str(stream.device)
        self._stream = stream
        self._stream_output = devices.default_output_id()
        self.output_device = name
        log.info("playing on %s", name)

    def _close_stream(self) -> None:
        if self._stream is None:
            return
        try:
            self._stream.stop()
            self._stream.close()
        except Exception:  # noqa: BLE001
            pass
        self._stream = None

    def _ensure_stream(self, new_reply: bool = False) -> None:
        self._last_device_check = time.time()
        current = devices.default_output_id()
        if self._stream is None:
            self._open_stream()
        elif current is None:
            # No cheap way to see the default here (not macOS): reopen per reply.
            if new_reply:
                self._open_stream()
        elif current != self._stream_output:
            self._open_stream()

    def _write(self, block: np.ndarray) -> None:
        if time.time() - self._last_device_check > 0.5:
            self._ensure_stream()
        try:
            self._stream.write(block)
        except Exception:  # noqa: BLE001 - device unplugged mid-write: reopen once
            log.warning("speaker write failed; reopening on the current default output")
            self._open_stream()
            self._stream.write(block)

    def _run(self) -> None:
        was_idle = True
        while True:
            try:
                gen, chunk, voice, speed, index = self._queue.get(timeout=0.1)
            except queue.Empty:
                if self._queue.empty():
                    self._idle.set()
                    was_idle = True
                continue
            with self._lock:
                if gen != self._generation:
                    continue  # cancelled by /stop while waiting in the queue
                self._stop_playback.clear()
                reading = self.reading if index >= 0 else None
                if reading is not None:
                    reading.at = index
            try:
                self._ensure_stream(new_reply=was_idle)
                was_idle = False
            except Exception as exc:  # noqa: BLE001 - keep the worker alive
                self.last_error = f"could not open the speaker: {exc}"
                log.exception(self.last_error)
                self._stream = None
                continue
            interrupted = False
            try:
                for audio in self._generate(chunk, voice, speed):
                    if self._play(audio):
                        interrupted = True
                        break
                self.last_error = ""
            except urllib.error.HTTPError as exc:
                detail = exc.read()[:200].decode(errors="ignore")
                self.last_error = f"{voice.split(':')[0]} speech failed: HTTP {exc.code} {detail}"
                log.error(self.last_error)
                self._fallback(chunk, voice, speed)
            except Exception as exc:  # noqa: BLE001 - keep the worker alive
                self.last_error = f"{voice.split(':')[0]} speech failed: {exc}"
                log.exception("synthesis failed for chunk: %r", chunk[:80])
                self._fallback(chunk, voice, speed)
            if reading is not None and not interrupted and not self._stop_playback.is_set():
                if index == len(reading.pieces) - 1 and self.reading is reading:
                    self.reading = None  # read to the end
                else:
                    reading.at = index + 1  # resume after the piece just finished
            # Tiny pause between sentences reads more naturally than butting them together.
            if not self._stop_playback.is_set():
                try:
                    self._write(np.zeros(int(SAMPLE_RATE * 0.12), dtype=np.float32))
                except Exception:  # noqa: BLE001
                    log.exception("speaker unavailable")
                    self._stream = None

    def _fallback(self, chunk: str, engine_voice: str, speed: float) -> None:
        """A cloud engine failed: say why with Kokoro, then read the text with Kokoro."""
        engine = engine_voice.split(":")[0]
        if engine == "kokoro":
            return
        reason = "it needs an API key" if "environment variable" in self.last_error else "the request failed"
        notice = f"The {engine} voice is not available, {reason}. Using Kokoro instead."
        try:
            for text in (notice, chunk):
                for audio in self._generate(text, f"kokoro:{self.cfg['voice']}", speed):
                    if self._play(audio):
                        return
        except Exception:  # noqa: BLE001
            log.exception("kokoro fallback failed")

    def _play(self, audio: np.ndarray) -> bool:
        """Write audio in small blocks; returns True if interrupted."""
        for i in range(0, len(audio), BLOCK):
            if self._stop_playback.is_set():
                return True
            self._write(np.ascontiguousarray(audio[i : i + BLOCK]))
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

        def _read(self, body: dict) -> None:
            if body.get("resume"):
                ok = speaker.resume()
                self._json(200 if ok else 409, {"ok": ok, "reading": speaker.reading.state() if speaker.reading else None,
                                                "error": "" if ok else "nothing to resume"})
                return
            try:
                if body.get("path"):
                    start, end = body.get("start"), body.get("end")
                    doc = load(str(body["path"]), start=int(start) if start else None, end=int(end) if end else None)
                    title, text, pages = doc.title, doc.text, doc.pages
                else:
                    title, text, pages = str(body.get("title") or "selection"), prepare_text(str(body.get("text", ""))), 0
            except DocumentError as exc:
                self._json(400, {"ok": False, "error": str(exc)})
                return
            except Exception as exc:  # noqa: BLE001 - say what went wrong rather than drop the request
                log.exception("could not read %s", body.get("path") or "the text")
                self._json(500, {"ok": False, "error": f"could not read it: {exc}"})
                return
            n = speaker.read(title, text, voice=body.get("voice"), speed=body.get("speed"), engine=body.get("engine"))
            if n == 0:
                self._json(400, {"ok": False, "error": f"{title} has nothing to read"})
                return
            self._json(200, {"ok": True, "title": title, "pieces": n, "pages": pages})

        def do_GET(self):
            if self.path == "/health":
                live = config.load()
                self._json(200, {"ok": True, "speaking": speaker.speaking, "voice": live["voice"],
                                 "engine": live.get("tts_engine", "kokoro"), "last_error": speaker.last_error,
                                 "output_device": speaker.output_device,
                                 "reading": speaker.reading.state() if speaker.reading else None,
                                 "root": ROOT})
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
            elif self.path == "/read":
                self._read(body)
            elif self.path == "/pause":
                speaker.pause()
                self._json(200, {"ok": True, "reading": speaker.reading.state() if speaker.reading else None})
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
