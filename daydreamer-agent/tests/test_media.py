from array import array
import cmath
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from daydreamer_agent.domain.errors import ValidationError
from daydreamer_agent.media.ffmpeg import Media
from daydreamer_agent.providers.audio import stage_audio
from daydreamer_agent.storage.files import write_json


class MediaIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.media = Media(ROOT)
        if not cls.media.ffmpeg or not cls.media.ffprobe:
            raise unittest.SkipTest("FFmpeg/FFprobe unavailable")
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        cls.root = Path(cls.temp.name)
        cls.clip = cls.root / "source.mp4"
        m = cls.media
        # Source includes a 1500 Hz soundtrack that must never reach the final output.
        m.execute([m.ffmpeg, "-nostdin", "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=blue:s=854x480:r=30",
                   "-f", "lavfi", "-i", "sine=frequency=1500:duration=3", "-t", "3", "-c:v", "libx264", "-threads", "2", "-c:a", "aac", str(cls.clip)])
        for name, frequency, duration in (("music.wav", 220, 0.5), ("effect.wav", 880, 0.3)):
            m.execute([m.ffmpeg, "-nostdin", "-y", "-v", "error", "-f", "lavfi", "-i", f"sine=frequency={frequency}:duration={duration}", str(cls.root / name)])
        cls.manifest = cls.root / "audio.json"
        write_json(cls.manifest, {"contains_speech": False, "music": {"path": "music.wav", "gain": 0.15}, "sfx": [{"path": "effect.wav", "at_seconds": 1.1, "gain": 0.4}]})
        tracks = stage_audio(cls.manifest, cls.root, 3, m)
        cls.parameters = {"duration": 3, "ratio": "16:9", "resolution": "480P"}
        cls.final = m.compose(cls.root, [(cls.clip, 3)], tracks, cls.parameters)

    def test_final_duration_dimensions_and_audio(self):
        info = self.media.check_clip(self.final, self.parameters, require_audio=True)
        self.assertAlmostEqual(info["duration"], 3, delta=0.05)
        self.assertEqual((info["width"], info["height"]), (854, 480))

    def tone_amplitude(self, at, frequency):
        result = subprocess.run([self.media.ffmpeg, "-nostdin", "-v", "error", "-ss", str(at), "-i", str(self.final), "-t", "0.1", "-vn", "-ac", "1", "-ar", "8000", "-f", "s16le", "-"], capture_output=True, check=True)
        samples = array("h", result.stdout)
        if sys.byteorder != "little":
            samples.byteswap()
        return abs(sum(sample / 32768 * cmath.exp(-2j * cmath.pi * frequency * i / 8000) for i, sample in enumerate(samples))) * 2 / len(samples)

    def test_original_audio_removed_and_effect_delayed(self):
        self.assertLess(self.tone_amplitude(0.5, 1500), 0.001)
        self.assertGreater(self.tone_amplitude(0.5, 220), 0.005)
        before = self.tone_amplitude(0.5, 880)
        after = self.tone_amplitude(1.15, 880)
        self.assertGreater(after, 0.01)
        self.assertGreater(after, before * 5)

    def test_preview_has_no_audio(self):
        preview = self.media.compose(self.root, [(self.clip, 3)], [], self.parameters, preview=True)
        self.assertFalse(self.media.check_clip(preview, self.parameters)["has_audio"])

    def test_speech_declaration_required(self):
        invalid = self.root / "invalid-audio.json"
        write_json(invalid, {"music": {"path": "music.wav"}, "sfx": []})
        with self.assertRaises(ValidationError):
            stage_audio(invalid, self.root, 3, self.media)

    def test_tail_matches_final_concat_boundary_after_trimming(self):
        m = self.media
        work = self.root / "chain"
        work.mkdir(exist_ok=True)
        source, prepared, tail = (work / name for name in ("source.mp4", "prepared.mp4", "tail.png"))
        m.execute([m.ffmpeg, "-nostdin", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=854x480:r=24",
                   "-t", "3.083333", "-c:v", "libx264", "-threads", "2", str(source)])
        m.prepare_clip(source, prepared, self.parameters)
        m.extract_tail(prepared, tail, 3)
        final = m.compose(work, [(prepared, 3), (prepared, 3)], [], {**self.parameters, "duration": 6}, preview=True, prepared=True)
        def pixels(path, frame):
            return subprocess.run([m.ffmpeg, "-nostdin", "-v", "error", "-i", str(path), "-vf", f"select=eq(n\\,{frame})",
                                   "-frames:v", "1", "-pix_fmt", "rgb24", "-f", "rawvideo", "-"], capture_output=True, check=True).stdout
        self.assertEqual(len(pixels(tail, 0)), 854 * 480 * 3)
        self.assertEqual(pixels(tail, 0), pixels(final, 89))
        self.assertNotEqual(pixels(tail, 0), pixels(source, 73))
        info = m.check_clip(final, {**self.parameters, "duration": 6})
        self.assertEqual(info["fps"], 30)
        self.assertFalse(info["has_audio"])

    def test_effect_outside_timeline_rejected(self):
        invalid = self.root / "invalid-position.json"
        write_json(invalid, {"contains_speech": False, "music": {"path": "music.wav"}, "sfx": [{"path": "effect.wav", "at_seconds": 4}]})
        with self.assertRaises(ValidationError):
            stage_audio(invalid, self.root, 3, self.media)


if __name__ == "__main__":
    unittest.main()
