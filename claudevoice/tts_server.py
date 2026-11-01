"""Local HTTP server that keeps Kokoro loaded and plays speech through the speakers.

Loading Kokoro takes a few seconds, so the Stop hook talks to this always-on
server instead of loading the model on every reply.

Several chats share this one server and one set of speakers, so every request names its
chat (`owner`). Each chat has its own speech and its own place in a reading, and stopping or
pausing one chat leaves the others alone. One sentence plays at a time, so voices never overlap:
the chat on screen (`front`) goes first, then the rest in the order they asked.

Endpoints (all on 127.0.0.1):
  GET  /health[?owner=C]  -> {"ok": true, "speaking": bool, "busy": bool, "held": bool,
                              "reading": {title, at, of, paused} | null, "voice": str, ...}
                             speaking and reading are that chat's; busy is any chat's voice;
                             held: that chat has speech paused, to resume.
  POST /speak             -> {"text": str, "owner"?: str, "front"?: bool, "voice"?: str,
                              "speed"?: float, "interrupt"?: bool, "wait"?: bool, "raw"?: bool}
                             raw=true skips markdown cleanup.
  POST /stop              -> {"owner"?: str, "current"?: bool}: cut that chat's speech and forget
                             its reading; current also cuts whichever chat is playing. With no
                             owner, everything stops.
  POST /read              -> {"path": str, "start"?: int, "end"?: int} or {"text": str, "title"?: str},
                             plus "owner"/"front"/"voice"/"speed"/"engine"; or {"resume": true,
                             "owner"?: str}. Reads a file (markdown, text, PDF) or selected text
                             sentence by sentence, keeping its place. start/end are lines, or
                             pages for a PDF.
  POST /pause             -> {"owner"?: str}: stop that chat's voice, keeping its place for
                             {"resume": true}
  POST /front             -> {"owner": str, "front": bool}: whether that chat is on screen
  POST /synthesize        -> same body as /speak; returns a WAV instead of playing
  POST /shutdown
"""
from __future__ import annotations

import io
import json
import logging
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
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
    """Speech for one chat: its pieces, and the one playing (or to resume from).

    A document has a title; a reply or a short phrase has none.
    """

    def __init__(self, owner: str, title: str, pieces: list[str], engine_voice: str, speed: float,
                 front: bool, order: int) -> None:
        self.owner, self.title, self.pieces = owner, title, pieces
        self.engine_voice, self.speed = engine_voice, speed
        self.front, self.order = front, order
        self.at = 0
        self.paused = False

    @property
    def active(self) -> bool:
        return not self.paused and self.at < len(self.pieces)

    def state(self) -> dict:
        return {"title": self.title, "at": self.at, "of": len(self.pieces), "paused": self.paused}


class Speaker:
    """Owns the Kokoro pipeline, each chat's speech, and the audio output stream."""

    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg
        self._speech: dict[str, Reading] = {}  # by chat: a reply or phrase being spoken
        self._docs: dict[str, Reading] = {}  # by chat: a document being read (or paused)
        self._front: dict[str, bool] = {}  # by chat: on screen, as it last said
        self._playing: Reading | None = None
        self._order = 0
        self._stop_playback = threading.Event()
        self._wake = threading.Event()
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
        """Any chat's voice is playing or about to."""
        with self._lock:
            return self._playing is not None or any(r.active for r in self._all())

    def speaking_for(self, owner: str) -> bool:
        with self._lock:
            return any(r.active for r in self._of(owner))

    def held(self, owner: str) -> bool:
        """This chat has speech paused, to resume."""
        with self._lock:
            return any(r.paused and r.at < len(r.pieces) for r in self._of(owner))

    def reading_of(self, owner: str) -> Reading | None:
        with self._lock:
            return self._docs.get(owner)

    def speak(self, text: str, *, voice: str | None, speed: float | None, interrupt: bool,
              engine: str | None = None, owner: str = "", front: bool = True) -> int:
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
        with self._lock:
            if interrupt:
                self._halt(owner)  # a reading cut off this way can be resumed
                self._speech.pop(owner, None)
            if not chunks:
                return 0
            log.info("speak %d chars as %s:%s", len(text), engine, voice)
            current = self._speech.get(owner)
            if current is not None:
                current.pieces.extend(chunks)  # sentences streaming in: they follow on
            else:
                self._speech[owner] = self._new(owner, "", chunks, f"{engine}:{voice}", speed, front)
        self._wake.set()
        return len(chunks)

    def read(self, title: str, text: str, *, voice: str | None, speed: float | None,
             engine: str | None = None, owner: str = "", front: bool = True) -> int:
        """Read cleaned document text aloud from the start, replacing what this chat was saying."""
        self.stop(owner)
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
        with self._lock:
            self._docs[owner] = self._new(owner, title, pieces, f"{engine}:{voice}", speed, front)
        self._wake.set()
        return len(pieces)

    def pause(self, owner: str = "") -> None:
        with self._lock:
            self._halt(owner)

    def resume(self, owner: str = "", front: bool | None = None) -> bool:
        """Carry on with this chat's paused speech, else its paused reading."""
        with self._lock:
            if front is not None:
                self._set_front(owner, front)
            for held in (self._speech.get(owner), self._docs.get(owner)):
                if held is not None and held.paused and held.at < len(held.pieces):
                    held.paused = False
                    self._wake.set()
                    return True
            return any(r.active for r in self._of(owner))

    def set_front(self, owner: str, front: bool) -> None:
        with self._lock:
            self._set_front(owner, front)

    def stop(self, owner: str | None = None, current: bool = False) -> None:
        """Cut this chat's speech and forget its reading (None: every chat's).
        `current` also cuts whichever chat is playing: you talked over it."""
        with self._lock:
            if current and self._playing is not None:
                self._forget(self._playing.owner)
            for chat in ([owner] if owner is not None else list({*self._speech, *self._docs})):
                self._forget(chat)

    def wait(self, timeout: float | None = None) -> bool:
        deadline = None if timeout is None else time.time() + timeout
        while self.speaking:
            if deadline is not None and time.time() >= deadline:
                return False
            time.sleep(0.05)
        return True

    # -- each chat's speech (call these holding the lock) -------------------------
    def _all(self) -> list[Reading]:
        return [*self._speech.values(), *self._docs.values()]

    def _of(self, owner: str) -> list[Reading]:
        return [r for r in (self._speech.get(owner), self._docs.get(owner)) if r is not None]

    def _new(self, owner: str, title: str, pieces: list[str], engine_voice: str, speed: float, front: bool) -> Reading:
        self._order += 1
        self._front[owner] = front
        return Reading(owner, title, pieces, engine_voice, speed, front, self._order)

    def _set_front(self, owner: str, front: bool) -> None:
        self._front[owner] = front
        for reading in self._of(owner):
            reading.front = front

    def _halt(self, owner: str) -> None:
        """Cut this chat's voice; its speech keeps its place, paused."""
        for reading in self._of(owner):
            reading.paused = True
        if self._playing is not None and self._playing.owner == owner:
            self._stop_playback.set()

    def _forget(self, owner: str) -> None:
        self._halt(owner)
        self._speech.pop(owner, None)
        self._docs.pop(owner, None)

    def _pick(self) -> Reading | None:
        """The next speech to play a sentence of: the chat on screen first, then the oldest."""
        with self._lock:
            active = [r for r in self._all() if r.active]
            if not active:
                self._playing = None
                return None
            self._playing = min(active, key=lambda r: (not r.front, r.order))
            self._stop_playback.clear()
            return self._playing

    def _played(self, reading: Reading, index: int, interrupted: bool) -> None:
        """A sentence ended. Heard to its end, the place moves on; the last one ends the speech."""
        with self._lock:
            self._playing = None
            if interrupted or self._stop_playback.is_set() or reading.at != index:
                return
            reading.at = index + 1
            if reading.at >= len(reading.pieces):
                for slots in (self._speech, self._docs):
                    if slots.get(reading.owner) is reading:
                        del slots[reading.owner]

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
            reading = self._pick()
            if reading is None:
                was_idle = True
                self._wake.wait(0.1)
                self._wake.clear()
                continue
            index, chunk = reading.at, reading.pieces[reading.at]
            voice, speed = reading.engine_voice, reading.speed
            try:
                self._ensure_stream(new_reply=was_idle)
                was_idle = False
            except Exception as exc:  # noqa: BLE001 - keep the worker alive
                self.last_error = f"could not open the speaker: {exc}"
                log.exception(self.last_error)
                self._stream = None
                self._played(reading, index, False)  # move on rather than retry this forever
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
            stopped = interrupted or self._stop_playback.is_set()
            self._played(reading, index, stopped)
            # Tiny pause between sentences reads more naturally than butting them together.
            if not stopped:
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
            owner = str(body.get("owner") or "")
            if body.get("resume"):
                ok = speaker.resume(owner, body.get("front"))
                reading = speaker.reading_of(owner)
                self._json(200 if ok else 409, {"ok": ok, "reading": reading.state() if reading else None,
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
            n = speaker.read(title, text, voice=body.get("voice"), speed=body.get("speed"), engine=body.get("engine"),
                             owner=owner, front=body.get("front", True) is not False)
            if n == 0:
                self._json(400, {"ok": False, "error": f"{title} has nothing to read"})
                return
            self._json(200, {"ok": True, "title": title, "pieces": n, "pages": pages})

        def do_GET(self):
            url = urllib.parse.urlsplit(self.path)
            if url.path == "/health":
                live = config.load()
                # No chat named: anything playing, and the first reading there is.
                owner = urllib.parse.parse_qs(url.query).get("owner", [None])[0]
                busy = speaker.speaking
                reading = speaker.reading_of(owner or "")
                self._json(200, {"ok": True, "speaking": speaker.speaking_for(owner) if owner else busy, "busy": busy,
                                 "held": speaker.held(owner or ""), "voice": live["voice"],
                                 "engine": live.get("tts_engine", "kokoro"), "last_error": speaker.last_error,
                                 "output_device": speaker.output_device,
                                 "reading": reading.state() if reading else None,
                                 "root": ROOT})
            else:
                self._json(404, {"error": "not found"})

        def do_POST(self):
            body = self._body()
            owner = str(body.get("owner") or "")
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
                    owner=owner,
                    front=body.get("front", True) is not False,
                )
                if body.get("wait"):
                    speaker.wait()
                self._json(200, {"ok": True, "chunks": n, "text": text})
            elif self.path == "/stop":
                speaker.stop(owner if owner else None, current=bool(body.get("current")))
                self._json(200, {"ok": True})
            elif self.path == "/read":
                self._read(body)
            elif self.path == "/pause":
                speaker.pause(owner)
                reading = speaker.reading_of(owner)
                self._json(200, {"ok": True, "reading": reading.state() if reading else None})
            elif self.path == "/front":
                speaker.set_front(owner, body.get("front", True) is not False)
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
