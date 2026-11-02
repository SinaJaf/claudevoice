"""The voice server's speech, without Kokoro or a speaker: each sentence "plays" for 40 ms.

Run: .venv/bin/python -m unittest tests.test_speaker
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

os.environ.setdefault("CLAUDEVOICE_HOME", tempfile.mkdtemp())

import numpy as np  # noqa: E402

from claudevoice import config, tts_server  # noqa: E402

LONG = " ".join(f"Sentence number {i} of the long text, going on for a while." for i in range(40))


class Silent(tts_server.Speaker):
    def __init__(self, cfg: dict) -> None:
        super().__init__(cfg)
        self.played: list[str] = []
        self.made: list[str] = []
        self.making = 0.0  # seconds each piece takes to make

    def _generate(self, text, engine_voice, speed):
        self.made.append(text)
        time.sleep(self.making)
        yield np.zeros(tts_server.BLOCK * 2, dtype=np.float32)

    def _pick(self):
        reading = super()._pick()
        if reading is not None:
            self.played.append(reading.pieces[reading.at])
        return reading

    def _ensure_stream(self, new_reply=False):
        pass

    def _write(self, block):
        time.sleep(0.02)


class SpeakerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.speaker = Silent(config.load())
        cls.speaker.start_threads()
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), tts_server.make_handler(cls.speaker, config.load()))
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        cls.dir = tempfile.mkdtemp()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()

    def setUp(self) -> None:
        self.speaker.stop()
        self.wait_until(lambda: not self.speaker.speaking)
        self.speaker.played.clear()
        self.speaker.made.clear()
        self.speaker.making = 0.0

    def call(self, path: str, body: dict | None = None) -> dict:
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.base + path, data=data, method="GET" if body is None else "POST")
        try:
            with urllib.request.urlopen(req) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            return {"status": e.code, **json.loads(e.read())}

    def health(self, owner: str) -> dict:
        return self.call(f"/health?owner={owner}")

    def wait_until(self, check, seconds: float = 5.0) -> None:
        deadline = time.time() + seconds
        while time.time() < deadline:
            if check():
                return
            time.sleep(0.01)
        self.fail("timed out")

    def write(self, name: str, text: str) -> str:
        path = os.path.join(self.dir, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return path

    # ---- making speech ahead ------------------------------------------------------

    def test_speech_is_made_ahead_of_what_is_playing(self) -> None:
        self.call("/speak", {"text": LONG, "owner": "a"})
        self.wait_until(lambda: len(self.speaker.made) >= tts_server.AHEAD)
        self.assertLessEqual(len(self.speaker.played), 2, "made before it is reached")
        self.assertLessEqual(len(self.speaker.made), len(self.speaker.played) + tts_server.AHEAD, "only a few ahead")

    def test_a_stop_does_not_wait_for_the_next_piece_to_be_made(self) -> None:
        self.speaker.making = 0.1  # a piece takes longer to make than the one before takes to play
        waits: list[float] = []
        original = self.speaker._next_audio
        def timed(take, at):
            t0 = time.time()
            audio = original(take, at)
            if at == 0 and audio is not None:
                waits.append(time.time() - t0)
            return audio
        self.speaker._next_audio = timed
        try:
            self.call("/speak", {"text": LONG, "owner": "a"})
            self.wait_until(lambda: len(waits) >= 4, 15)
        finally:
            del self.speaker._next_audio
        self.assertLess(max(waits[:4]), 0.05, "already made when its turn came")

    def test_stopping_ends_the_making_too(self) -> None:
        self.call("/speak", {"text": LONG, "owner": "a"})
        self.wait_until(lambda: len(self.speaker.made) >= 1)
        self.call("/stop", {"owner": "a"})
        self.wait_until(lambda: not self.speaker.speaking)
        made = len(self.speaker.made)
        time.sleep(0.15)
        self.assertLessEqual(len(self.speaker.made), made + 1, "at most the one already under way")

    def test_a_paused_chat_makes_nothing_more(self) -> None:
        self.call("/speak", {"text": LONG, "owner": "a"})
        self.wait_until(lambda: len(self.speaker.played) >= 1)
        self.call("/pause", {"owner": "a"})
        time.sleep(0.05)
        made = len(self.speaker.made)
        time.sleep(0.15)
        self.assertEqual(len(self.speaker.made), made)

    def test_a_voice_that_cannot_start_says_so_once_and_kokoro_reads_on(self) -> None:
        said: list[str] = []

        def generate(text, voice, speed):
            if voice.startswith("qwen:"):
                raise RuntimeError("the graphics card is not available")
            said.append(text)
            yield np.zeros(tts_server.BLOCK, dtype=np.float32)

        self.speaker._generate = generate
        try:
            self.call("/read", {"text": "The first paragraph goes here.\n\nThe second paragraph follows it.",
                                "owner": "a", "engine": "qwen", "voice": "Aiden"})
            self.wait_until(lambda: not self.speaker.speaking)
        finally:
            del self.speaker._generate
        self.assertEqual(len(said), 3, said)
        self.assertIn("The Qwen voice is not available", said[0])
        self.assertTrue(said[1].startswith("The first paragraph"))
        self.assertTrue(said[2].startswith("The second paragraph"))
        self.assertIn("graphics card", self.speaker.last_error)

    # ---- chats do not touch each other ------------------------------------------

    def test_stopping_one_chat_leaves_the_other(self) -> None:
        self.call("/speak", {"text": LONG, "owner": "a"})
        self.call("/speak", {"text": LONG, "owner": "b"})
        self.assertTrue(self.health("a")["speaking"])
        self.assertTrue(self.health("b")["speaking"])
        self.call("/stop", {"owner": "a"})
        self.assertFalse(self.health("a")["speaking"])
        self.assertTrue(self.health("b")["speaking"])
        self.assertTrue(self.health("a")["busy"], "a still hears that another chat's voice is playing")

    def test_a_new_reply_in_one_chat_does_not_cut_the_other(self) -> None:
        self.call("/speak", {"text": LONG, "owner": "a"})
        self.call("/speak", {"text": "A reply from the other chat, short and sweet.", "owner": "b"})
        self.call("/speak", {"text": "A later reply from the first chat, replacing its own.", "owner": "a"})
        self.assertTrue(self.health("b")["speaking"])
        self.wait_until(lambda: not self.speaker.speaking)
        self.assertIn("A reply from the other chat, short and sweet.", self.speaker.played)
        self.assertIn("A later reply from the first chat, replacing its own.", self.speaker.played)

    def test_voices_take_turns_sentence_by_sentence_and_never_overlap(self) -> None:
        overlaps = []
        original, playing = self.speaker._play, [0]

        def play(audio):
            playing[0] += 1
            if playing[0] > 1:
                overlaps.append(True)
            try:
                return original(audio)
            finally:
                playing[0] -= 1

        self.speaker._play = play
        try:
            self.call("/speak", {"text": "One long sentence coming from the first chat here. And its second long sentence coming now.", "owner": "a"})
            self.call("/speak", {"text": "One long sentence coming from the second chat here. And its second long sentence coming now.", "owner": "b"})
            self.wait_until(lambda: not self.speaker.speaking)
        finally:
            del self.speaker._play
        self.assertEqual(overlaps, [])
        self.assertEqual(len(self.speaker.played), 4)

    def test_the_chat_on_screen_goes_first(self) -> None:
        self.call("/speak", {"text": LONG, "owner": "away", "front": False})
        self.wait_until(lambda: len(self.speaker.played) >= 1)
        self.call("/speak", {"text": "The chat you are in answers right after this sentence ends.", "owner": "here", "front": True})
        heard_before = len(self.speaker.played)
        self.wait_until(lambda: "The chat you are in answers right after this sentence ends." in self.speaker.played)
        self.assertLessEqual(len(self.speaker.played), heard_before + 2)
        self.assertTrue(self.health("away")["speaking"], "the other chat carries on afterwards")

    def test_coming_on_screen_moves_a_chat_to_the_front(self) -> None:
        self.call("/speak", {"text": LONG, "owner": "first", "front": False})
        self.call("/speak", {"text": "A second chat waiting for its turn right now.", "owner": "second", "front": False})
        self.wait_until(lambda: len(self.speaker.played) >= 2)
        self.assertNotIn("A second chat waiting for its turn right now.", self.speaker.played)
        self.call("/front", {"owner": "second", "front": True})
        self.wait_until(lambda: "A second chat waiting for its turn right now." in self.speaker.played)

    # ---- leaving a chat and coming back --------------------------------------------

    def test_a_paused_reply_carries_on_where_it_stopped(self) -> None:
        self.call("/speak", {"text": LONG, "owner": "a"})
        self.wait_until(lambda: len(self.speaker.played) >= 2)
        self.call("/pause", {"owner": "a"})
        state = self.health("a")
        self.assertFalse(state["speaking"])
        self.assertTrue(state["held"])
        self.wait_until(lambda: not self.speaker.speaking)
        heard = list(self.speaker.played)
        self.assertEqual(self.call("/read", {"resume": True, "owner": "a"})["ok"], True)
        self.wait_until(lambda: len(self.speaker.played) > len(heard))
        self.assertEqual(self.speaker.played[len(heard)], heard[-1], "the sentence it stopped in, again")
        self.assertFalse(self.health("a")["held"])

    def test_pausing_one_chat_leaves_the_other_playing(self) -> None:
        self.call("/speak", {"text": LONG, "owner": "a"})
        self.call("/speak", {"text": LONG, "owner": "b"})
        self.call("/pause", {"owner": "a"})
        self.assertFalse(self.health("a")["speaking"])
        self.assertTrue(self.health("b")["speaking"])

    def test_talking_over_another_chats_voice_stops_it(self) -> None:
        self.call("/speak", {"text": LONG, "owner": "other"})
        self.wait_until(lambda: len(self.speaker.played) >= 1)
        self.call("/stop", {"owner": "mine", "current": True})
        self.wait_until(lambda: not self.speaker.speaking)

    # ---- reading a file ------------------------------------------------------------

    def test_reading_keeps_its_place_per_chat(self) -> None:
        path = self.write("long.md", LONG)
        self.assertTrue(self.call("/read", {"path": path, "owner": "a"})["ok"])
        self.assertTrue(self.call("/read", {"path": path, "owner": "b"})["ok"])
        self.wait_until(lambda: self.health("a")["reading"]["at"] > 0 or self.health("b")["reading"]["at"] > 0)
        self.call("/pause", {"owner": "a"})
        a = self.health("a")["reading"]
        self.assertTrue(a["paused"])
        self.assertFalse(self.health("b")["reading"]["paused"])
        self.assertEqual(self.health("other")["reading"], None)
        self.call("/read", {"resume": True, "owner": "a"})
        self.assertFalse(self.health("a")["reading"]["paused"])

    def test_a_reply_cutting_in_pauses_that_chats_reading(self) -> None:
        path = self.write("long2.md", LONG)
        self.call("/read", {"path": path, "owner": "a"})
        self.call("/speak", {"text": "A reply cuts in here, just for a moment.", "owner": "a"})
        self.wait_until(lambda: "A reply cuts in here, just for a moment." in self.speaker.played)
        self.wait_until(lambda: not self.speaker.speaking)
        self.assertTrue(self.health("a")["reading"]["paused"])
        self.assertTrue(self.call("/read", {"resume": True, "owner": "a"})["ok"])

    def test_stop_forgets_the_reading_and_resume_says_so(self) -> None:
        self.call("/read", {"text": "One short sentence here, read as a selection. And a second one.", "owner": "a"})
        self.call("/stop", {"owner": "a"})
        self.assertEqual(self.health("a")["reading"], None)
        self.assertEqual(self.call("/read", {"resume": True, "owner": "a"})["status"], 409)

    def test_reading_to_the_end_clears_it(self) -> None:
        self.call("/read", {"text": "One short sentence here, read as a selection.", "title": "selection", "owner": "a"})
        self.wait_until(lambda: self.health("a")["reading"] is None and not self.health("a")["speaking"])

    def test_refusals_say_why(self) -> None:
        missing = os.path.join(self.dir, "missing.md")
        binary = os.path.join(self.dir, "data.bin")
        with open(binary, "wb") as f:
            f.write(b"\0\1\2")
        for body, why in [({"path": missing}, "was not found"), ({"path": binary}, "not a text file"), ({"text": "   "}, "nothing to read")]:
            r = self.call("/read", body)
            self.assertEqual(r["status"], 400)
            self.assertIn(why, r["error"])

    # ---- callers that name no chat -----------------------------------------------------

    def test_with_no_chat_named_stop_stops_everything(self) -> None:
        self.call("/speak", {"text": LONG, "owner": "a"})
        self.call("/speak", {"text": LONG})
        self.assertTrue(self.call("/health")["speaking"])
        self.call("/stop", {})
        self.assertFalse(self.health("a")["speaking"])
        self.wait_until(lambda: not self.call("/health")["speaking"])

    def test_sentences_streaming_in_follow_on(self) -> None:
        self.call("/speak", {"text": "The first streamed sentence goes here now.", "interrupt": True})
        self.call("/speak", {"text": "The second streamed sentence follows it.", "interrupt": False})
        self.wait_until(lambda: not self.speaker.speaking)
        self.assertEqual(self.speaker.played[-2:], ["The first streamed sentence goes here now.", "The second streamed sentence follows it."])


if __name__ == "__main__":
    unittest.main()
