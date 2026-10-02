from copy import deepcopy
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

import test_frame_chain
from daydreamer_agent.domain.errors import ValidationError
from daydreamer_agent.media.ffmpeg import Media


class ClipToleranceTests(unittest.TestCase):
    def setUp(self):
        self.media = Media(test_frame_chain.ROOT)
        self.media.execute = Mock()
        self.probe = {"streams": [{"codec_type": "video", "duration": "6.583333", "avg_frame_rate": "24/1", "width": 1344, "height": 768}]}
        self.media.probe = Mock(side_effect=lambda _: deepcopy(self.probe))
        self.parameters = {"duration": 6, "ratio": "16:9", "resolution": "768P"}

    def test_long_source_passes_but_same_unedited_delivery_fails(self):
        info = self.media.check_clip(Path("clip.mp4"), self.parameters, source=True)
        self.assertAlmostEqual(info["duration"], 6.583333)
        with self.assertRaisesRegex(ValidationError, "剪辑成片.*实际6.583秒"):
            self.media.check_clip(Path("clip.mp4"), self.parameters)

    def test_edited_output_allows_point_three_seconds_in_both_directions(self):
        for value in (5.7, 6, 6.3):
            self.probe["streams"][0]["duration"] = str(value)
            self.media.check_clip(Path("clip.mp4"), self.parameters)
        for value in (5.69, 6.31):
            self.probe["streams"][0]["duration"] = str(value)
            with self.assertRaises(ValidationError): self.media.check_clip(Path("clip.mp4"), self.parameters)

    def test_source_tolerance_is_bounded_and_does_not_hide_bad_fps_or_size(self):
        for value in (5.69, 6.91, float("nan")):
            self.probe["streams"][0]["duration"] = str(value)
            with self.assertRaises(ValidationError): self.media.check_clip(Path("clip.mp4"), self.parameters, source=True)
        self.probe["streams"][0]["duration"] = "6.5"
        self.probe["streams"][0]["avg_frame_rate"] = "0/1"
        with self.assertRaisesRegex(ValidationError, "帧率"): self.media.check_clip(Path("clip.mp4"), self.parameters, source=True)
        self.probe["streams"][0].update(avg_frame_rate="24/1", width=768)
        with self.assertRaisesRegex(ValidationError, "画幅"): self.media.check_clip(Path("clip.mp4"), self.parameters, source=True)


class ClipRecoveryTests(unittest.TestCase):
    def setUp(self):
        test_frame_chain.FrameChainTests.setUp(self)

    def fail_second(self):
        original = self.pipeline.media.check_clip
        def check(path, parameters, **kwargs):
            if "shot-02" in str(path): raise ValidationError("旧时长校验失败")
            return original(path, parameters, **kwargs)
        with patch.object(self.pipeline.media, "check_clip", side_effect=check):
            with self.assertRaises(ValidationError): self.pipeline.render(self.run)
        self.assertEqual(self.video.submissions, 2)

    def test_failed_download_is_reused_without_post_query_or_download(self):
        self.fail_second()
        state = self.store.shot(self.run, "shot-02")
        self.assertEqual(state["remote_status"], "SUCCEEDED")
        self.assertEqual(state["failure"], "technical_validation")
        with patch.object(self.video, "submit") as submit, patch.object(self.video, "query") as query, patch.object(self.video, "download") as download:
            self.assertEqual(self.pipeline.render(self.run)["status"], "awaiting_audio")
            submit.assert_not_called(); query.assert_not_called(); download.assert_not_called()
        recovered = self.store.shot(self.run, "shot-02")
        self.assertEqual((recovered["attempt"], recovered["task_id"]), (state["attempt"], state["task_id"]))
        self.assertNotIn("failure", recovered)
        self.assertTrue((self.path / recovered["tail_frame"]).is_file())

    def test_corrupted_cached_clip_is_not_silently_reused(self):
        self.fail_second()
        state = self.store.shot(self.run, "shot-02")
        (self.path / state["clip"]).write_bytes(b"modified")
        with self.assertRaisesRegex(ValidationError, "原片发生变化"):
            self.pipeline.render(self.run)
        self.assertEqual(self.video.submissions, 2)

    def test_still_invalid_clip_stays_failed_without_new_billable_task(self):
        self.fail_second()
        with patch.object(self.pipeline.media, "check_clip", side_effect=ValidationError("视频仍不符合要求")):
            with self.assertRaises(ValidationError): self.pipeline.render(self.run)
        self.assertEqual(self.store.shot(self.run, "shot-02")["status"], "failed")
        self.assertEqual(self.video.submissions, 2)


class ActualTailTests(unittest.TestCase):
    def test_tail_uses_real_last_frame_even_when_duration_differs(self):
        media = Media(test_frame_chain.ROOT)
        if not media.ffmpeg or not media.ffprobe: self.skipTest("FFmpeg unavailable")
        with tempfile.TemporaryDirectory() as directory:
            video, tail = Path(directory) / "edited.mp4", Path(directory) / "tail.png"
            media.execute([media.ffmpeg, "-nostdin", "-v", "error", "-f", "lavfi", "-i", "color=c=red:s=320x180:r=30:d=4",
                           "-f", "lavfi", "-i", "color=c=blue:s=320x180:r=30:d=0.2", "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0[v]",
                           "-map", "[v]", "-c:v", "libx264", "-threads", "2", str(video)])
            media.check_clip(video, {"duration": 4, "ratio": "16:9", "resolution": "180P"})
            media.extract_tail(video, tail, 4)
            pixels = subprocess.run([media.ffmpeg, "-v", "error", "-i", str(tail), "-vf", "scale=1:1", "-frames:v", "1",
                                     "-pix_fmt", "rgb24", "-f", "rawvideo", "-"], capture_output=True, check=True).stdout
            self.assertGreater(pixels[2], 180)
            self.assertLess(pixels[0], 70)
