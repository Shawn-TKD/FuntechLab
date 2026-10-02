"""Native soundtrack preservation and recovery; all providers are offline fakes."""
from array import array
from copy import deepcopy
import cmath
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import test_frame_chain
from test_workflow import WorkflowMedia
from test_creativity import CreativeProvider
from daydreamer_agent import cli
from daydreamer_agent.application.settings import audio_policy, load_settings
from daydreamer_agent.domain.errors import ValidationError
from daydreamer_agent.media.ffmpeg import Media
from daydreamer_agent.prompts.video_compiler import compile_shot
from daydreamer_agent.providers.video_models import H3
from daydreamer_agent.storage.files import file_hash, read_json, write_json
from daydreamer_agent.story.assembly import generate_assembly
from daydreamer_agent.story.validation import validate_story

ROOT = test_frame_chain.ROOT


class RecordingMedia(WorkflowMedia):
    def __init__(self):
        self.calls = []

    def compose(self, path, clips, tracks, parameters, **kwargs):
        self.calls.append((deepcopy(kwargs), list(clips)))
        return super().compose(path, clips, tracks, parameters, **kwargs)


class NativeWorkflowTests(unittest.TestCase):
    def setUp(self):
        test_frame_chain.FrameChainTests.setUp(self)
        inputs = read_json(self.path / "input/normalized.json")
        self.story = read_json(self.path / "story/result.json")
        inputs["production_constraints"].update(video_model=H3, resolution="768P", duration_seconds=8)
        for shot in self.story["shots"]:
            shot["duration_seconds"] = 4
            shot["audio"]["music"] = "本镜不使用背景音乐。"
        self.config = load_settings(ROOT)
        self.inputs = inputs
        self.run = self.store.create(inputs, self.config)
        self.path = self.store.path(self.run)
        write_json(self.path / "input/skill.json", {})
        self.pipeline.finish_story(self.run, self.story)
        self.media = RecordingMedia()
        self.pipeline.media = self.media

    def test_native_finishes_without_external_manifest_and_reuses_cached_output(self):
        result = self.pipeline.run_full(self.run, wait_seconds=0)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(self.video.submissions, 2)
        options, clips = self.media.calls[0]
        self.assertTrue(options["prepared"])
        self.assertTrue(all(p.name == "prepared.mp4" for p, _ in clips))
        self.assertTrue(all(p.name == "clip.mp4" for p in options["native_sources"]))
        manifest = read_json(self.path / "manifest.json")
        self.assertEqual(manifest["audio_mode"], "native")
        self.assertEqual(manifest["audio"], [])
        self.assertFalse(manifest["media_content_checked"])
        self.pipeline.run_full(self.run)
        self.assertEqual((self.video.submissions, len(self.media.calls)), (2, 1))

    def test_changed_final_is_rebuilt_locally(self):
        result = self.pipeline.run_full(self.run, wait_seconds=0)
        Path(result["final"]).write_bytes(b"corrupted delivery")
        self.pipeline.run_full(self.run)
        self.assertEqual((self.video.submissions, len(self.media.calls)), (2, 2))

    def test_switching_to_preview_and_back_preserves_frame_chain(self):
        self.pipeline.run_full(self.run, wait_seconds=0)
        previous = deepcopy(self.store.shot(self.run, self.story["shots"][1]["shot_id"])["predecessor"])
        frozen = file_hash(self.path / "input/config.json")
        self.assertEqual(self.pipeline.run_full(self.run, audio_mode="off")["status"], "preview_ready")
        self.assertNotIn("native_sources", self.media.calls[-1][0])
        self.assertEqual(self.pipeline.run_full(self.run, audio_mode="native")["status"], "completed")
        self.assertEqual(self.video.submissions, 2)
        self.assertEqual(self.store.shot(self.run, self.story["shots"][1]["shot_id"])["predecessor"], previous)
        self.assertEqual(file_hash(self.path / "input/config.json"), frozen)

    def test_old_h3_preview_can_opt_in_without_new_video_or_new_boundary(self):
        frozen = deepcopy(self.config)
        frozen["audio"].pop("mode")
        frozen["audio"]["preserve_generated_clip_audio"] = False
        run = self.store.create(self.inputs, frozen)
        write_json(self.store.path(run) / "input/skill.json", {})
        self.pipeline.finish_story(run, self.story)
        self.assertEqual(self.pipeline.run_full(run, wait_seconds=0)["status"], "preview_ready")
        second = self.store.shot(run, self.story["shots"][1]["shot_id"])
        self.assertEqual(self.pipeline.run_full(run, audio_mode="native")["status"], "completed")
        self.assertEqual(self.video.submissions, 2)
        self.assertEqual(second, self.store.shot(run, self.story["shots"][1]["shot_id"]))

    def test_manual_missing_manifest_waits_and_conflicts_do_not_submit(self):
        self.assertEqual(self.pipeline.run_full(self.run, audio_mode="manual", wait_seconds=0)["status"], "awaiting_audio")
        self.assertEqual(len(self.media.calls), 0)
        manifest = self.path / "manual.json"
        write_json(manifest, {})
        with self.assertRaises(ValidationError):
            self.pipeline.run_full(self.run, audio_mode="native", audio_manifest=manifest)
        self.assertEqual(self.video.submissions, 2)

    def test_native_failure_recovers_without_video_requests(self):
        with patch.object(self.media, "compose", side_effect=ValidationError("missing native track")):
            with self.assertRaises(ValidationError):
                self.pipeline.run_full(self.run, wait_seconds=0)
        job = self.store.load(self.run)
        self.assertEqual((job["status"], job["failed_stage"]), ("failed", "composition"))
        self.assertEqual(self.pipeline.run_full(self.run)["status"], "completed")
        self.assertEqual(self.video.submissions, 2)

    def test_policy_rejects_native_for_unsupported_model_and_invalid_silent_ids(self):
        config = deepcopy(self.config)
        config["video"]["model"] = "wan3.0-video-prime"
        with self.assertRaises(ValidationError): audio_policy(config)
        self.assertEqual(audio_policy(config, "off")[0], "off")
        for values in ("shot-01", ["../bad"], ["shot-01", "shot-01"]):
            config["audio"]["silent_shots"] = values
            with self.assertRaises(ValidationError): audio_policy(config, "off")

    def test_current_music_decision_is_not_overridden_by_global_direction(self):
        self.story["audio_plan"]["music_direction"] = "GLOBAL_MUSIC_SHOULD_NOT_BE_COPIED"
        shot = self.story["shots"][0]
        validate_story(self.story, self.inputs)
        prompt = compile_shot(self.story, shot, self.inputs["production_constraints"], H3)["request"]["input"]["prompt"]
        self.assertIn("【背景音乐】\n本镜不使用背景音乐。", prompt)
        self.assertIn(shot["audio"]["sfx"][0], prompt)
        self.assertNotIn("GLOBAL_MUSIC_SHOULD_NOT_BE_COPIED", prompt)
        self.assertEqual(set(shot["prompt"]), {"景别", "构图", "运镜手法", "画面内容"})

    def test_shot_context_contains_neighbor_audio_without_extra_generation(self):
        provider = CreativeProvider(self.story)
        prior = {key: {key: self.story[key]} for key in ("theme", "script", "visual_style")}
        result = generate_assembly(self.path / "new-sound", self.inputs, prior, provider, 1, 1, lambda _: None, sound_version=1)
        self.assertEqual(len(provider.calls), 3)  # Existing plan + two shots only.
        first, second = [ctx for ctx, _ in provider.calls if ctx["stage"] == "shot"]
        self.assertIsNone(first["previous_audio"])
        self.assertEqual(second["previous_audio"], result["shots"][0]["audio"])
        self.assertIsNotNone(first["next_action_range"])
        self.assertIsNone(second["next_action_range"])
        generate_assembly(self.path / "new-sound", self.inputs, prior, provider, 1, 1, lambda _: None, sound_version=1)
        self.assertEqual(len(provider.calls), 3)

    def test_cli_explicit_manual_choice_is_frozen_without_mutating_defaults(self):
        args = cli.parser().parse_args(["--project", str(ROOT), "run", "--events", str(ROOT / "examples/events.json"),
                                       "--resolution", "768P", "--audio-mode", "off"])
        run = cli.new_run(args, self.config, self.store)
        self.assertEqual(read_json(self.store.path(run) / "input/config.json")["audio"]["mode"], "off")
        self.assertEqual(self.config["audio"]["mode"], "native")


class NativeMediaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.media = Media(ROOT)
        if not cls.media.ffmpeg or not cls.media.ffprobe:
            raise unittest.SkipTest("FFmpeg unavailable")
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        cls.root = Path(cls.temp.name)
        cls.parameters = {"duration": 3, "ratio": "16:9", "resolution": "480P"}
        cls.sources, cls.prepared = [], []
        for index, frequency in enumerate((880, 1320)):
            source, prepared = cls.root / f"raw-{index}.mp4", cls.root / f"prepared-{index}.mp4"
            cls.media.execute([cls.media.ffmpeg, "-nostdin", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=854x480:r=24",
                               "-f", "lavfi", "-i", f"aevalsrc=if(between(t\\,0.8\\,1.2)\\,0.125*sin(2*PI*{frequency}*t)\\,0):s=48000:d=3.2",
                               "-t", "3.2", "-c:v", "libx264", "-threads", "2", "-c:a", "aac", str(source)])
            cls.media.prepare_clip(source, prepared, cls.parameters)
            cls.sources.append(source)
            cls.prepared.append((prepared, 3))
        cls.final = cls.media.compose(cls.root, cls.prepared, [], {**cls.parameters, "duration": 6}, prepared=True, native_sources=cls.sources)

    def amplitude(self, path, at, frequency):
        output = subprocess.run([self.media.ffmpeg, "-nostdin", "-v", "error", "-ss", str(at), "-i", str(path), "-t", "0.1",
                                 "-vn", "-ac", "1", "-ar", "8000", "-f", "s16le", "-"], capture_output=True, check=True)
        samples = array("h", output.stdout)
        if sys.byteorder != "little": samples.byteswap()
        return abs(sum(s / 32768 * cmath.exp(-2j * cmath.pi * frequency * i / 8000) for i, s in enumerate(samples))) * 2 / len(samples)

    def test_original_effects_survive_at_correct_positions_and_silent_gaps_stay_silent(self):
        info = self.media.check_clip(self.final, {**self.parameters, "duration": 6}, require_audio=True)
        self.assertAlmostEqual(info["duration"], 6, delta=0.05)
        for at, frequency in ((0.9, 880), (3.9, 1320)):
            self.assertGreater(self.amplitude(self.final, at, frequency), 0.02)
            self.assertLess(self.amplitude(self.final, at - 0.5, frequency), 0.001)
            self.assertLess(self.amplitude(self.final, at + 0.6, frequency), 0.001)
        self.assertFalse(self.media.check_clip(self.prepared[0][0], self.parameters)["has_audio"])

    def test_visual_frames_are_unchanged_by_native_sound(self):
        def frame(path, index):
            return subprocess.run([self.media.ffmpeg, "-nostdin", "-v", "error", "-i", str(path), "-vf", f"select=eq(n\\,{index})",
                                   "-frames:v", "1", "-pix_fmt", "rgb24", "-f", "rawvideo", "-"], capture_output=True, check=True).stdout
        self.assertEqual(frame(self.final, 89), frame(self.prepared[0][0], 89))
        self.assertEqual(frame(self.final, 90), frame(self.prepared[1][0], 0))

    def test_missing_audio_is_an_error_but_explicit_silent_shot_is_supported(self):
        work = self.root / "missing"
        work.mkdir(exist_ok=True)
        with self.assertRaisesRegex(ValidationError, "缺少音轨"):
            self.media.compose(work, self.prepared, [], {**self.parameters, "duration": 6}, prepared=True,
                               native_sources=[self.prepared[0][0], self.sources[1]])
        final = self.media.compose(work, self.prepared, [], {**self.parameters, "duration": 6}, prepared=True,
                                   native_sources=[None, self.sources[1]])
        self.assertLess(self.amplitude(final, 0.9, 880), 0.001)
        self.assertGreater(self.amplitude(final, 3.9, 1320), 0.02)

    def test_audio_start_offset_is_preserved(self):
        work = self.root / "offset"
        work.mkdir(exist_ok=True)
        source = work / "source.mp4"
        self.media.execute([self.media.ffmpeg, "-nostdin", "-y", "-v", "error", "-f", "lavfi", "-i", "color=s=854x480:r=30",
                            "-itsoffset", "0.5", "-f", "lavfi", "-i", "sine=frequency=960:duration=2.5", "-t", "3",
                            "-c:v", "libx264", "-threads", "2", "-c:a", "aac", str(source)])
        final = self.media.compose(work, [(source, 3)], [], self.parameters, native_sources=[source])
        self.assertLess(self.amplitude(final, 0.2, 960), 0.001)
        self.assertGreater(self.amplitude(final, 0.7, 960), 0.02)


if __name__ == "__main__":
    unittest.main()
