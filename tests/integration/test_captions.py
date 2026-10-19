"""Automatic captions (F17). The speech model is replaced by a stand-in that "hears" fixed words, so
these tests need FFmpeg but not faster-whisper or a model download."""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
import wave
from unittest.mock import patch

from core.clip import Clip, MediaType
from core.transcribe import (
    INSTALL_HINT,
    MAX_LINE_CHARS,
    Word,
    group_words,
    transcribable_problem,
    transcribe_clip,
)
from tests.integration import test_p2_real_ffmpeg as p2

FFMPEG = p2.FFMPEG
_TMP = None
SPEECH = None

WORDS = [Word(0.2, 0.5, " Hello"), Word(0.5, 0.9, " there,"), Word(0.9, 1.3, " this"), Word(1.3, 1.5, " is"),
         Word(1.5, 2.0, " a"), Word(2.0, 2.6, " test."), Word(4.0, 4.4, " After"), Word(4.4, 5.0, " silence.")]


def setUpModule():
    global _TMP, SPEECH
    if not FFMPEG:
        return
    _TMP = tempfile.mkdtemp(prefix="ve_captions_")
    SPEECH = os.path.join(_TMP, "speech.mp4")
    subprocess.run([FFMPEG, "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=gray:s=160x90:r=25:d=6",
                    "-f", "lavfi", "-i", "sine=frequency=300:sample_rate=44100:d=6", "-c:v", "libx264",
                    "-preset", "ultrafast", "-c:a", "aac", "-shortest", SPEECH], check=True, capture_output=True)


def tearDownModule():
    if _TMP:
        shutil.rmtree(_TMP, ignore_errors=True)


class _Ears:
    """Stands in for faster-whisper: checks the WAV it is given, then returns WORDS."""

    def __init__(self):
        self.seen = None

    def __call__(self, wav_path):
        with wave.open(wav_path) as wav:
            self.seen = (wav.getnchannels(), wav.getframerate(), wav.getnframes() / wav.getframerate())
        return list(WORDS)


class GroupingTests(unittest.TestCase):
    def test_pauses_and_length_start_new_cues_and_long_text_wraps(self):
        cues = group_words(WORDS)
        self.assertEqual([c.text for c in cues], ["Hello there, this is a test.", "After silence."])
        self.assertEqual((cues[0].start, cues[0].end), (0.2, 2.6))
        many = [Word(i * 0.3, i * 0.3 + 0.25, f" word{i:02d}") for i in range(30)]
        long_cues = group_words(many)
        self.assertGreater(len(long_cues), 1)
        for cue in long_cues:
            lines = cue.text.split("\n")
            self.assertLessEqual(len(lines), 2)
            self.assertTrue(all(len(line) <= MAX_LINE_CHARS + 8 for line in lines))
            self.assertLessEqual(cue.end - cue.start, 6.0 + 1e-9)

    def test_what_can_be_transcribed(self):
        self.assertIsNotNone(transcribable_problem(None))
        self.assertIsNotNone(transcribable_problem(Clip(media_type=MediaType.TEXT)))
        self.assertIn("backwards", transcribable_problem(Clip(media_type=MediaType.VIDEO, reverse=True)))
        self.assertIsNone(transcribable_problem(Clip(media_type=MediaType.AUDIO)))
        self.assertIn("pip install faster-whisper", INSTALL_HINT)


@unittest.skipUnless(FFMPEG, "FFmpeg not found")
class TranscribeClipTests(unittest.TestCase):
    def test_cues_land_on_the_timeline_through_the_trim_and_speed(self):
        clip = Clip(media_type=MediaType.VIDEO, source_path=SPEECH, source_in=1.0, source_out=6.0,
                    timeline_start=10.0, source_duration=6.0, speed=2.0)
        ears = _Ears()
        cues = transcribe_clip(clip, words_from=ears, ffmpeg=FFMPEG)
        channels, rate, seconds = ears.seen
        self.assertEqual((channels, rate), (1, 16000))
        self.assertAlmostEqual(seconds, 5.0, delta=0.05)  # only the trimmed range
        self.assertEqual(len(cues), 2)
        self.assertAlmostEqual(cues[0].start, 10.0 + 0.2 / 2, places=3)  # 2x speed halves the times
        self.assertAlmostEqual(cues[1].end, 10.0 + 5.0 / 2, places=3)

    def test_generate_captions_adds_a_subtitles_track_in_one_step(self):
        try:
            from PySide6.QtWidgets import QApplication
        except ImportError:
            self.skipTest("PySide6 not installed")
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        app = QApplication.instance() or QApplication([])
        home = tempfile.mkdtemp(prefix="ve_home_")
        with patch.dict(os.environ, {"USERPROFILE": home, "HOME": home}), \
                patch("ui.main_window.nvenc_available", return_value=False):
            from ui.main_window import MainWindow

            window = MainWindow()
            window._autosave_timer.stop()
            try:
                clip = Clip(media_type=MediaType.VIDEO, source_path=SPEECH, source_out=6.0, timeline_start=0.0,
                            source_duration=6.0, name="talk")
                window.project.tracks[0].clips.append(clip)
                window.timeline.rebuild()
                window.timeline.select_clips([clip.id])
                tracks_before = len(window.project.tracks)
                self.assertEqual(window.generate_captions("tiny", None, words_from=_Ears()), 2)
                subtitles = window.project.tracks[-1]
                self.assertEqual(subtitles.name, "Subtitles")
                self.assertEqual([c.text_style.content for c in subtitles.sorted_clips()],
                                 ["Hello there, this is a test.", "After silence."])
                window.undo()
                self.assertEqual(len(window.project.tracks), tracks_before)
                with patch("core.transcribe.whisper_available", return_value=False), \
                        patch("ui.main_window.QMessageBox.information") as info:
                    self.assertEqual(window.generate_captions("tiny", None), 0)
                    self.assertIn("faster-whisper", info.call_args[0][2])
            finally:
                window._clear_autosave()
                window.deleteLater()
                app.processEvents()
        shutil.rmtree(home, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()


class WordCaptionTests(unittest.TestCase):
    """F33: one word at a time, and the translate switch reaching faster-whisper."""

    def test_one_cue_per_word_held_until_the_next_word(self):
        cues = group_words(WORDS, "words")
        self.assertEqual([c.text for c in cues][:3], ["Hello", "there,", "this"])
        self.assertEqual(len(cues), len(WORDS))
        self.assertEqual((cues[0].start, cues[0].end), (0.2, 0.5))  # until "there," starts
        self.assertAlmostEqual(cues[5].end, 2.6 + 0.4)  # "test." held 0.4 s into the pause, not to 4.0
        for first, second in zip(cues, cues[1:]):
            self.assertLessEqual(first.end, second.start + 1e-9)  # never overlapping
        short = group_words([Word(1.0, 1.02, "a"), Word(3.0, 3.5, "b")], "words")
        self.assertAlmostEqual(short[0].end - short[0].start, 0.42)

    @unittest.skipUnless(FFMPEG, "FFmpeg not available")
    def test_word_style_and_translate_through_transcribe_clip(self):
        clip = Clip(media_type=MediaType.VIDEO, source_path=SPEECH, source_out=6.0, timeline_start=10.0, source_duration=6.0)
        cues = transcribe_clip(clip, words_from=_Ears(), style="words")
        self.assertEqual(len(cues), len(WORDS))
        self.assertAlmostEqual(cues[0].start, 10.2)
        import types

        seen = {}

        class FakeModel:
            def __init__(self, *args, **kwargs):
                pass

            def transcribe(self, wav, **kwargs):
                seen.update(kwargs)
                segment = types.SimpleNamespace(words=[types.SimpleNamespace(start=0.1, end=0.4, word=" Hi")],
                                                text=" Hi", start=0.1, end=0.4)
                return iter([segment]), None

        fake = types.ModuleType("faster_whisper")
        fake.WhisperModel = FakeModel
        with patch.dict("sys.modules", {"faster_whisper": fake}):
            cues = transcribe_clip(clip, "tiny", "es", translate=True)
        self.assertEqual(seen["task"], "translate")
        self.assertEqual([c.text for c in cues], ["Hi"])
