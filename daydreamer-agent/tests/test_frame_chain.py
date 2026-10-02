import base64
from copy import deepcopy
from pathlib import Path
import struct
import sys
import tempfile
import unittest
import zlib

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from daydreamer_agent.application.pipeline import Pipeline
from legacy_settings import load_settings
from daydreamer_agent.domain.continuity import STATE_FIELDS
from daydreamer_agent.domain.errors import ValidationError
from daydreamer_agent.domain.events import normalize
from daydreamer_agent.providers.wan import prepare_submission
from daydreamer_agent.prompts.video_compiler import compile_shot
from daydreamer_agent.domain.continuity import state_description
from daydreamer_agent.storage.files import file_hash, read_json, write_json
from daydreamer_agent.storage.jobs_sqlite import JobStore
from test_pipeline import FakeMedia, FakeVideo


class ChainMedia(FakeMedia):
    def prepare_clip(self, source, target, parameters):
        target.write_bytes(source.read_bytes() + b"prepared")

    def extract_tail(self, source, target, duration):
        def chunk(kind, data):
            return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
        target.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 854, 480, 8, 2, 0, 0, 0))
                          + chunk(b"IDAT", zlib.compress((b"\x00" + b"\x00" * (854 * 3)) * 480)) + chunk(b"IEND", b""))


class RecordingVideo(FakeVideo):
    def __init__(self):
        super().__init__()
        self.payloads = []

    def submit(self, request):
        self.payloads.append(deepcopy(request))
        return super().submit(request)


class FrameChainTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        inputs = normalize(read_json(ROOT / "examples/events.json"))
        inputs["production_constraints"]["continuity_mode"] = "frame_chain"
        story = read_json(ROOT / "examples/story.json")
        state = dict(zip(STATE_FIELDS, ["门前", "向前", "缓慢前行", "门在右侧", "手自然垂下", "暖色灯光"]))
        for shot in story["shots"]:
            shot["continuity"] = {"start": deepcopy(state), "end": deepcopy(state)}
        self.store = JobStore(root / "runs", root / "jobs.sqlite3")
        self.run = self.store.create(inputs, load_settings(ROOT))
        self.path = self.store.path(self.run)
        self.video = RecordingVideo()
        self.pipeline = Pipeline(self.store, video_provider=self.video, media=ChainMedia(), progress=lambda _: None)
        self.pipeline.finish_story(self.run, story)

    def test_exact_prepared_tail_is_sent_as_first_frame(self):
        self.pipeline.render(self.run)
        first, second = self.video.payloads
        self.assertEqual(first["model"], "wan3.0-video-prime")
        self.assertNotIn("media", first["input"])
        self.assertEqual(second["model"], "wan3.0-video-prime")
        self.assertEqual(second["parameters"]["ratio"], "adaptive")
        for payload in (first, second):
            self.assertFalse(payload["parameters"]["audio"])
            self.assertFalse(payload["parameters"]["prompt_extend"])
        self.assertNotIn("【首帧续接】", first["input"]["prompt"])
        self.assertIn("【首帧续接】", second["input"]["prompt"])
        prior = self.store.shot(self.run, "shot-01")
        following = self.store.shot(self.run, "shot-02")
        image = second["input"]["media"][0]
        self.assertEqual(image["type"], "first_frame")
        self.assertEqual(base64.b64decode(image["url"].split(",", 1)[1]), (self.path / prior["tail_frame"]).read_bytes())
        self.assertEqual(following["predecessor"], self.pipeline.dependency(prior))
        persisted = (self.path / "shots/shot-02/attempts/1/video_request.json").read_text(encoding="utf-8")
        self.assertNotIn("base64", persisted)
        self.assertIn("ratio", read_json(self.path / "shots/shot-02/video_request.json")["request"]["parameters"])
        self.pipeline.render(self.run)
        self.assertEqual(self.video.submissions, 2)

    def test_waits_for_previous_clip_and_resumes_same_remote_task(self):
        self.video.results = [{"task_status": "RUNNING"}]
        self.pipeline.render(self.run)
        self.assertEqual(self.video.submissions, 1)
        self.assertEqual(self.store.shot(self.run, "shot-02")["status"], "pending")
        self.pipeline.render(self.run)
        self.assertEqual(self.video.submissions, 2)
        self.assertEqual(self.video.queries[:2], ["task-1", "task-1"])

    def test_redo_invalidates_descendants_and_preserves_old_attempts(self):
        self.pipeline.render(self.run)
        self.pipeline.retry_shot(self.run, "shot-01", regenerate=True)
        for name in ("shot-01", "shot-02"):
            state = self.store.shot(self.run, name)
            self.assertEqual((state["status"], state["attempt"]), ("pending", 2))
            self.assertTrue((self.path / "shots" / name / "attempts/1/clip.mp4").exists())
        self.assertTrue(self.store.load(self.run)["outputs_stale"])
        self.pipeline.render(self.run)
        self.assertEqual(self.video.submissions, 4)
        self.assertEqual(self.store.shot(self.run, "shot-02")["predecessor"]["attempt"], 2)

    def test_redo_cannot_discard_active_descendant(self):
        self.video.results = [{"task_status": "SUCCEEDED", "video_url": "https://example.com/clip"}, {"task_status": "RUNNING"}]
        self.pipeline.render(self.run)
        with self.assertRaises(ValidationError):
            self.pipeline.retry_shot(self.run, "shot-01", regenerate=True)
        self.assertEqual(self.store.shot(self.run, "shot-01")["attempt"], 1)

    def test_mismatched_dependency_blocks_render_and_compose(self):
        self.pipeline.render(self.run)
        child = self.store.shot(self.run, "shot-02")
        child["predecessor"]["tail_sha256"] = "stale"
        self.store.save_shot(self.run, child)
        with self.assertRaises(ValidationError):
            self.pipeline.compose(self.run, preview=True)
        with self.assertRaises(ValidationError):
            self.pipeline.render(self.run)
        self.assertEqual(self.video.submissions, 2)
        self.pipeline.retry_shot(self.run, "shot-02", regenerate=True)
        self.pipeline.render(self.run)
        self.assertEqual(self.video.submissions, 3)

    def test_changed_prepared_clip_is_not_composed(self):
        self.pipeline.render(self.run)
        first = self.store.shot(self.run, "shot-01")
        (self.path / first["prepared_clip"]).write_bytes(b"changed")
        with self.assertRaises(ValidationError):
            self.pipeline.compose(self.run, preview=True)

    def test_i2v_requires_valid_frame_before_submission(self):
        request = read_json(self.path / "shots/shot-02/video_request.json")["request"]
        with self.assertRaises(ValidationError):
            prepare_submission(request, require_first_frame=True)
        invalid = self.path / "invalid.png"
        invalid.write_bytes(b"not an image")
        with self.assertRaises(ValidationError):
            prepare_submission(request, invalid)
        self.assertEqual(self.video.submissions, 0)

    def test_normalized_state_summary_is_not_duplicated_in_prompt(self):
        story = read_json(self.path / "story/result.json")
        inputs = read_json(self.path / "input/normalized.json")
        shot = story["shots"][0]
        shot["start_state"] = state_description(shot["continuity"]["start"])
        shot["end_state"] = state_description(shot["continuity"]["end"])
        prompt = compile_shot(story, shot, inputs["production_constraints"], "wan3.0-video-prime")["request"]["input"]["prompt"]
        self.assertNotIn("【起始状态】", prompt)
        self.assertNotIn("【结束状态】", prompt)
        self.assertIn("【连续状态】", prompt)
        for field, value in shot["continuity"]["start"].items():
            self.assertIn(field, prompt)
            self.assertIn(value, prompt)

    def test_retired_model_cannot_be_submitted(self):
        request = read_json(self.path / "shots/shot-01/video_request.json")["request"]
        request["model"] = "retired-video-model"
        with self.assertRaises(ValidationError):
            prepare_submission(request)

    def test_t2v_keeps_explicit_ratio_and_disables_audio_and_rewrite(self):
        request = read_json(self.path / "shots/shot-01/video_request.json")["request"]
        payload = prepare_submission(request)
        self.assertEqual(payload["parameters"]["ratio"], request["parameters"]["ratio"])
        self.assertNotIn("media", payload["input"])
        self.assertFalse(payload["parameters"]["audio"])
        self.assertFalse(payload["parameters"]["prompt_extend"])

    def test_alpha_first_frame_is_rejected(self):
        self.pipeline.render(self.run)
        first = self.store.shot(self.run, "shot-01")
        image = bytearray((self.path / first["tail_frame"]).read_bytes())
        image[25] = 6
        bad = self.path / "alpha.png"
        bad.write_bytes(image)
        request = read_json(self.path / "shots/shot-02/video_request.json")["request"]
        with self.assertRaises(ValidationError):
            prepare_submission(request, bad)


if __name__ == "__main__":
    unittest.main()
