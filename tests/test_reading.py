"""How text is prepared for the voice, and the pauses added to what it says. No model or speaker.

Run: .venv/bin/python -m unittest tests.test_reading
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace

import numpy as np

from claudevoice import pacing
from claudevoice.clean import StreamingSentenceSplitter, markdown_to_speech as spoken, split_sentences
from claudevoice.document import group, split_long

RATE = pacing.SAMPLE_RATE


def tone(seconds: float) -> np.ndarray:
    t = np.arange(int(RATE * seconds)) / RATE
    return (0.3 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)


def token(text: str, phonemes: str, start: float, end: float) -> SimpleNamespace:
    return SimpleNamespace(text=text, phonemes=phonemes, start_ts=start, end_ts=end)


def gaps(audio: np.ndarray) -> list[float]:
    """The lengths of the quiet between sounds, to 10 ms."""
    frame = RATE // 100
    quiet = [np.abs(audio[i : i + frame]).max() < 0.01 for i in range(0, len(audio) - frame, frame)]
    out, run = [], 0
    for q in quiet:
        if q:
            run += 1
        elif run:
            out.append(run / 100)
            run = 0
    return out[1:] if quiet and quiet[0] else out


class Headings(unittest.TestCase):
    def test_a_section_number_is_marked_off_from_its_title(self) -> None:
        for written in ("## 8 Reports and Analysis", "## 8. Reports and Analysis", "8 Reports and Analysis",
                        "**8 Reports and Analysis**", "8) Reports and Analysis"):
            self.assertEqual(spoken(written).rstrip("."), "Eight. Reports and Analysis", written)
        self.assertEqual(spoken("## 8.1 Overview"), "8.1. Overview.")

    def test_a_number_in_a_sentence_stays_where_it_is(self) -> None:
        for sentence in ("We have 8 reports and analysis here.", "3 people came to the meeting and stayed.",
                         "See section 8.2 for more."):
            self.assertEqual(spoken(sentence), sentence)

    def test_a_year_is_not_a_section_number(self) -> None:
        self.assertEqual(spoken("2024 Annual Report"), "2024 Annual Report")

    def test_a_list_keeps_its_numbers(self) -> None:
        self.assertEqual(spoken("1. Do this\n2) Do that\n- a bullet"), "One. Do this.\nTwo. Do that.\na bullet")
        self.assertEqual(spoken("21. Far down the list"), "21. Far down the list")

    def test_named_parts(self) -> None:
        self.assertEqual(spoken("Chapter 8 Reports and Analysis"), "Chapter 8. Reports and Analysis.")
        self.assertEqual(spoken("Chapter 8: Reports"), "Chapter 8: Reports")
        self.assertEqual(spoken("Chapter 8 covers the reports."), "Chapter 8 covers the reports.")

    def test_roman_numerals_read_as_numbers(self) -> None:
        self.assertEqual(spoken("Chapter VIII"), "Chapter 8")
        self.assertEqual(spoken("See Part IV for more."), "See Part 4 for more.")
        self.assertEqual(spoken("IV. Results"), "Four. Results.")
        self.assertEqual(spoken("Section C and Appendix V."), "Section C and Appendix V.")
        self.assertEqual(spoken("V. Putin spoke at length, then left."), "V. Putin spoke at length, then left.")

    def test_a_heading_stands_apart_from_the_text_below(self) -> None:
        pieces = split_sentences(spoken("## 8 Reports\nThe text goes on from here."))
        self.assertEqual(pieces, ["Eight. Reports.\n\n", "The text goes on from here."])
        self.assertEqual(pacing.pause_after(pieces[0]), pacing.PARAGRAPH)


class Marks(unittest.TestCase):
    def test_apostrophes(self) -> None:
        self.assertEqual(spoken("The dogs’ bowl, don’t."), "The dogs' bowl, don't.")

    def test_abbreviations(self) -> None:
        self.assertEqual(spoken("See Fig. 3, No. 8, pp. 4 and Sec. 2."), "See figure 3, number 8, pages 4 and section 2.")
        self.assertEqual(spoken("Some, e.g. this, i.e. that."), "Some, for example, this, that is, that.")
        self.assertEqual(spoken("Cats vs. dogs, etc. and so on."), "Cats versus dogs, et cetera and so on.")
        self.assertEqual(spoken("Cats, dogs, etc. They run."), "Cats, dogs, etc. They run.")

    def test_clocks_and_ratios(self) -> None:
        self.assertEqual(spoken("At 10:30, 10:05, 10:00."), "At 10 30, 10 oh 5, 10.")
        self.assertEqual(spoken("A ratio of 3:1 or 16:9."), "A ratio of 3 to 1 or 16 to 9.")
        self.assertEqual(spoken("It ran 1:23:45."), "It ran 1 23 45.")
        self.assertEqual(spoken("Open 127.0.0.1:8080 now."), "Open 127.0.0.1 8080 now.")

    def test_dashes(self) -> None:
        self.assertEqual(spoken("Pages 8-10 or 8–10."), "Pages 8 to 10 or 8 to 10.")
        self.assertEqual(spoken("One - two -- three—four."), "One — two — three — four.")
        self.assertEqual(spoken("Run it with --help now."), "Run it with --help now.")
        self.assertEqual(spoken("A well-known 2024-01-15, 555-123-4567."), "A well-known 2024-01-15, 555-123-4567.")
        self.assertEqual(spoken("5 - 3 is 2."), "5 minus 3 is 2.")

    def test_a_plural_in_parentheses(self) -> None:
        self.assertEqual(spoken("The file(s) here."), "The files here.")


class Technical(unittest.TestCase):
    def test_version_numbers_keep_their_dots(self) -> None:
        self.assertEqual(spoken("We are on version 0.5.1 now."), "We are on version 0 point 5 point 1 now.")
        self.assertEqual(spoken("Version 0.5.0 is out."), "Version 0 point 5 point 0 is out.")
        self.assertEqual(spoken("Install v0.5.1 today."), "Install version 0 point 5 point 1 today.")
        self.assertEqual(spoken("The version v1.2.3 works."), "The version 1 point 2 point 3 works.")
        self.assertEqual(spoken("Python 3.10 runs."), "Python 3.10 runs.")
        self.assertEqual(spoken("Open 127.0.0.1 now."), "Open 127.0.0.1 now.")

    def test_file_names_are_spelled(self) -> None:
        self.assertEqual(spoken("Open `pacing.py` and report.pdf."), "Open pacing dot P Y and report dot P D F.")
        self.assertEqual(spoken("See tts_server.py:42 now."), "See tts server dot P Y line 42 now.")
        self.assertEqual(spoken("Read README.md first."), "Read README dot M D first.")
        self.assertEqual(spoken("See claudevoice/pacing.py here."), "See pacing dot P Y here.")

    def test_keys(self) -> None:
        self.assertEqual(spoken("Press Ctrl+C or \u2318C to copy."), "Press control C or command C to copy.")
        self.assertEqual(spoken("Press \u2318 C."), "Press command C.")

    def test_commits_are_short_and_spelled(self) -> None:
        self.assertEqual(spoken("It is commit `7f3e676` now."), "It is commit 7 F 3 E 6 7 6 now.")
        self.assertEqual(spoken("Hash 7f3e676467da37c8ba8d23a4e9684a019b92449d here."), "Hash 7 F 3 E 6 7 6 here.")
        self.assertEqual(spoken("A deadbeef and a decade."), "A deadbeef and a decade.")

    def test_names_in_code_read_as_words(self) -> None:
        self.assertEqual(spoken("Then type `/reload-plugins` now."), "Then type reload plugins now.")
        self.assertEqual(spoken("Run it with `--help` now."), "Run it with dash dash help now.")
        self.assertEqual(spoken("A well-known name stays."), "A well-known name stays.")

    def test_lines_of_a_file(self) -> None:
        self.assertEqual(spoken("Type `@notes.md#L40-80` here."), "Type at notes dot M D line 40 to 80 here.")

    def test_at_signs(self) -> None:
        self.assertEqual(spoken("Pick (@notes.md, or @report.pdf)."), "Pick (at notes dot M D, or at report dot P D F).")
        self.assertEqual(spoken("Mail me@example.org now."), "Mail me@example.org now.")

    def test_signs_are_said_or_dropped(self) -> None:
        self.assertEqual(spoken("Press \u25cf Call now."), "Press Call now.")
        self.assertEqual(spoken("Copy \u2192 paste."), "Copy to paste.")
        self.assertEqual(spoken("See issue #12 now."), "See issue number 12 now.")


class Sentences(unittest.TestCase):
    def test_a_title_does_not_end_the_sentence(self) -> None:
        self.assertEqual(len(split_sentences("Dr. Smith went to the office to see Mr. Jones about the plan today.")), 1)
        self.assertEqual(len(split_sentences("J. Doe wrote a rather long letter to the U.S. Senate about all of it.")), 1)

    def test_a_line_never_runs_into_the_next(self) -> None:
        self.assertEqual(split_sentences("Short one.\nAnother.\n\nThird."), ["Short one.\n", "Another.\n\n", "Third."])

    def test_a_cut_piece_keeps_its_line_break(self) -> None:
        pieces = split_long(["word " * 200 + "end.\n\n"])
        self.assertTrue(pieces[-1].endswith("\n\n"))
        self.assertTrue(all(len(p) <= 402 for p in pieces))

    def test_grouping_keeps_paragraphs_apart(self) -> None:
        pieces = group(["One sentence.\n", "Two sentence.\n\n", "Three sentence.", "Four sentence."], 200)
        self.assertEqual(pieces, ["One sentence. Two sentence.\n\n", "Three sentence. Four sentence."])
        self.assertEqual(group(["A long one here.", "Another long one."], 20), ["A long one here.", "Another long one."])

    def test_streaming_knows_titles_too(self) -> None:
        splitter = StreamingSentenceSplitter(min_chars=5)
        self.assertEqual(splitter.feed("We asked Dr. Smith about it. Then we left"), ["We asked Dr. Smith about it."])


class Pauses(unittest.TestCase):
    def test_after_a_piece(self) -> None:
        self.assertEqual(pacing.pause_after("He left."), pacing.PAUSES["."])
        self.assertEqual(pacing.pause_after('"He left."'), pacing.PAUSES["."])
        self.assertEqual(pacing.pause_after("Then, in the end,"), pacing.PAUSES[","])
        self.assertEqual(pacing.pause_after("in the middle of a"), pacing.WORD_GAP)
        self.assertEqual(pacing.pause_after("A list item.\n"), pacing.LINE)
        self.assertEqual(pacing.pause_after("A paragraph.\n\n"), pacing.PARAGRAPH)
        self.assertEqual(pacing.pause_after("A paragraph.\n\n", 2.0), pacing.PARAGRAPH / 2)

    def colon(self, gap: float = 0.0, speed: float = 1.0) -> np.ndarray:
        audio = np.concatenate([tone(0.5), np.zeros(int(RATE * gap), dtype=np.float32), tone(0.5)])
        tokens = [token("Jack", "ʤˈæk", 0.0, 0.5), token(":", ":", 0.5, 0.5 + gap), token("hi", "hˈI", 0.5 + gap, 1.0 + gap)]
        return pacing.pace(audio, tokens, speed)

    def test_a_colon_gets_its_pause_where_there_was_none(self) -> None:
        self.assertAlmostEqual(gaps(self.colon())[0], pacing.PAUSES[":"], delta=0.03)

    def test_quiet_already_there_counts(self) -> None:
        self.assertAlmostEqual(gaps(self.colon(gap=0.2))[0], pacing.PAUSES[":"], delta=0.03)
        self.assertAlmostEqual(gaps(self.colon(gap=0.6))[0], 0.6, delta=0.03)  # never shortened

    def test_a_faster_voice_pauses_for_less(self) -> None:
        self.assertAlmostEqual(gaps(self.colon(speed=2.0))[0], pacing.PAUSES[":"] / 2, delta=0.03)

    def test_marks_run_together_take_the_longest(self) -> None:
        audio = np.concatenate([tone(0.5), tone(0.5)])
        tokens = [token("end", "ˈɛnd", 0, 0.45), token(")", ")", 0.45, 0.5), token(".", ".", 0.5, 0.5), token("Go", "ɡˈO", 0.5, 1.0)]
        self.assertAlmostEqual(gaps(pacing.pace(audio, tokens))[0], pacing.PAUSES["."], delta=0.03)

    def test_no_pause_before_the_first_word_or_after_the_last(self) -> None:
        audio = tone(1.0)
        tokens = [token("(", "(", 0.0, 0.0), token("hi", "hˈI", 0.0, 1.0), token(".", ".", 1.0, 1.0)]
        self.assertEqual(gaps(pacing.pace(audio, tokens)), [])

    def test_quotes_alone_add_nothing(self) -> None:
        audio = np.concatenate([tone(0.5), tone(0.5)])
        tokens = [token("said", "sˈɛd", 0, 0.5), token('"', "“", 0.5, 0.5), token("go", "ɡˌO", 0.5, 1.0)]
        self.assertEqual(gaps(pacing.pace(audio, tokens)), [])

    def test_the_ends_are_trimmed(self) -> None:
        audio = np.concatenate([np.zeros(RATE, dtype=np.float32), tone(0.5), np.zeros(RATE, dtype=np.float32)])
        self.assertLess(len(pacing.pace(audio, None)), RATE * 0.6)

    def test_a_stream_is_trimmed_at_both_ends(self) -> None:
        quiet = np.zeros(RATE // 2, dtype=np.float32)
        chunks = [quiet, np.concatenate([quiet, tone(0.2)]), tone(0.5), np.concatenate([tone(0.2), quiet])]
        out = list(pacing.trim_stream(chunks))
        self.assertEqual(len(out), 3, "the quiet chunk before speech is dropped")
        self.assertLess(sum(len(c) for c in out) / RATE, 1.0)
        self.assertGreater(sum(len(c) for c in out) / RATE, 0.9)

    def test_a_silent_stream_gives_nothing(self) -> None:
        self.assertEqual(list(pacing.trim_stream([np.zeros(RATE, dtype=np.float32)])), [])

    def test_nothing_but_quiet_is_left_alone(self) -> None:
        audio = np.zeros(RATE, dtype=np.float32)
        self.assertEqual(len(pacing.pace(audio, [])), RATE)


if __name__ == "__main__":
    unittest.main()
