from copy import deepcopy
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import test_event_extraction
import test_frame_chain
import test_workflow
from daydreamer_agent import cli
from daydreamer_agent.domain.continuity import STATE_FIELDS
from daydreamer_agent.domain.errors import ProviderError, ValidationError
from daydreamer_agent.storage.files import read_json, write_json
from daydreamer_agent.storage.jobs_sqlite import JobStore


class StoryProvider:
    calls = 0
    fail_once = False
    def generate(self, instruction, context):
        self.calls += 1
        if self.fail_once:
            self.fail_once = False
            raise ProviderError("offline interruption")
        event = context["input"]["event_cards"][0]
        story = json.loads((test_frame_chain.ROOT / "examples/story.json").read_text(encoding="utf-8").replace("demo-bike-01", event["event_id"]))
        state = dict(zip(STATE_FIELDS, ["路上", "向前", "平稳前进", "车把在下方", "双手握把", "暖色灯光"]))
        for shot in story["shots"]:
            shot["continuity"] = {"start": deepcopy(state), "end": deepcopy(state)}
            shot["duration_seconds"] = (context["input"]["production_constraints"]["duration_seconds"] or 24) // 2
            shot["source_refs"] = [event["source_refs"][0]]
        story["theme"]["reality_mapping"][0]["source_refs"] = [event["source_refs"][0]]
        stage = len(context["upstream"])
        if stage < 3:
            name = ("theme", "script", "visual_style")[stage]
            return {"status": "ready", name: story[name], "issues": []}, {}
        if context["input"]["production_constraints"].get("pacing_prompt_version"):
            story["timing_reason"] = {"reason": "按铃与继续骑行分别需要清楚的动作过程。", "extension_reason": "", "extension_beat_ids": []}
        return story, {}


class VideoWorkflowTests(unittest.TestCase):
    def setUp(self):
        test_event_extraction.ExtractionTests.setUp(self)
        self.config["creativity"]["enabled"] = False  # These tests isolate the extraction/generation bridge.
        self.story = StoryProvider()
        self.video = test_frame_chain.RecordingVideo()
        self.media = test_workflow.WorkflowMedia()
        self.media.probe = lambda _: {"streams": [{"index": 0, "codec_type": "video", "duration": "6", "width": 640, "height": 360}]}
        for target, value in (("load_settings", self.config), ("Media", self.media),
                              ("Qwen", self.story), ("BailianVideo", self.video), ("load_skill", {})):
            patcher = patch.object(cli, target, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.object(cli.Credentials, "load", return_value=object())
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch("daydreamer_agent.application.video_workflow.QwenVision", return_value=self.provider)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.generation_store = JobStore(self.root / self.config["storage"]["run_directory"], self.root / self.config["storage"]["task_database"])

    def invoke(self, *flags):
        args = cli.parser().parse_args(["--project", str(self.root), "run", "--wait", "0", *flags])
        with patch("sys.stdout", new_callable=io.StringIO):
            return cli.run_command(args)

    def full_run(self):
        return self.invoke("--video", str(self.source))

    def test_native_end_to_end_and_explicit_off_resume_keep_same_child(self):
        self.config["video"].update(model="MiniMax/MiniMax-H3", continuation_model="MiniMax/MiniMax-H3")
        self.config["workflow"]["resolution"] = "768P"
        self.config["audio"].update(mode="native", preserve_generated_clip_audio=True)
        result = self.invoke("--video", str(self.source), "--audio-mode", "native")
        self.assertEqual(result["status"], "completed")
        final = read_json(Path(result["delivery"]["directory"]) / "本轮运行记录.json")
        self.assertEqual(final["audio_source"], "native")
        counts = (len(self.provider.calls), self.story.calls, self.video.submissions)
        preview = self.invoke("--run", result["run_id"], "--audio-mode", "off")
        self.assertEqual(preview["status"], "preview_ready")
        self.assertEqual(preview["generation_run_id"], result["generation_run_id"])
        # A later plain resume retains the deliberate off selection.
        again = self.invoke("--run", result["run_id"])
        self.assertEqual(again["status"], "preview_ready")
        self.assertEqual((len(self.provider.calls), self.story.calls, self.video.submissions), counts)

    def test_source_video_to_final_delivery_and_resume_reuses_all_models(self):
        result = self.full_run()
        self.assertEqual(result["status"], "preview_ready")
        self.assertEqual((len(self.provider.calls), self.story.calls, self.video.submissions), (1, 4, 2))
        target = Path(result["delivery"]["directory"])
        self.assertTrue(Path(result["delivery"]["video"]).is_file())
        self.assertEqual(read_json(target / "生活事件卡.json"), read_json(result["event_card"]))
        self.assertTrue((target / "素材提取关联.json").is_file())
        parent = self.store.load(result["run_id"])
        self.assertEqual(parent["generation_run_id"], result["generation_run_id"])
        # Existing extracted facts remain truthful; user-selected full workflow is recorded separately.
        card = read_json(result["event_card"])
        self.assertEqual(card["selection_info"]["status"], "waiting_accumulation")
        self.assertIsNone(card["selection_info"]["is_duplicate"])
        self.source.unlink()
        resumed = self.invoke("--run", result["run_id"])
        self.assertEqual(resumed["generation_run_id"], result["generation_run_id"])
        self.assertEqual((len(self.provider.calls), self.story.calls, self.video.submissions), (1, 4, 2))
        self.assertEqual(len(self.generation_store.list()), 1)

    def test_source_video_automatically_enters_seeded_creation(self):
        from test_creativity import CreativeProvider
        self.config["creativity"]["enabled"] = True
        class BridgeCreative:
            delegate = None
            def generate(inner, instruction, context, **sampling):
                if inner.delegate is None:
                    template, _ = StoryProvider().generate(instruction, {**context, "upstream": {"a": 1, "b": 2, "c": 3}})
                    inner.delegate = CreativeProvider(template)
                return inner.delegate.generate(instruction, context, **sampling)
        provider = BridgeCreative()
        with patch.object(cli, "Qwen", return_value=provider):
            result = self.full_run()
            self.assertEqual(result["status"], "preview_ready")
            path = self.generation_store.path(result["generation_run_id"])
            self.assertTrue((path / "input/creativity.json").is_file())
            self.assertTrue((Path(result["delivery"]["directory"]) / "创作随机种子.json").is_file())
            self.assertEqual(len(provider.delegate.calls), 6)
            self.assertTrue(all("seed" in sampling for _, sampling in provider.delegate.calls))
            self.invoke("--run", result["run_id"])
            self.assertEqual(len(provider.delegate.calls), 6)

    def test_story_failure_resumes_same_linked_generation_task(self):
        self.story.fail_once = True
        with self.assertRaises(ProviderError):
            self.full_run()
        parent = next(job for job in self.store.list() if job.get("generation_run_id"))
        child = parent["generation_run_id"]
        result = self.invoke("--run", parent["run_id"])
        self.assertEqual(result["generation_run_id"], child)
        self.assertEqual(len(self.generation_store.list()), 1)
        self.assertEqual((len(self.provider.calls), self.video.submissions), (1, 2))

    def test_running_video_resumes_original_remote_task(self):
        self.video.results = [{"task_status": "RUNNING"}]
        first = self.full_run()
        self.assertEqual(self.video.submissions, 1)
        result = self.invoke("--run", first["run_id"])
        self.assertEqual(result["status"], "preview_ready")
        self.assertEqual(self.video.queries[:2], ["task-1", "task-1"])
        self.assertEqual(self.video.submissions, 2)

    def test_no_event_never_starts_story_or_video(self):
        self.provider.responses = [{"content": json.dumps({"status": "no_event", "issues": ["无有效事件"], "card": None}), "finish_reason": "stop", "usage": {}}]
        result = self.full_run()
        self.assertEqual(result["status"], "no_event")
        self.assertEqual(result["workflow_stage"], "extraction")
        self.assertEqual(self.story.calls, 0)
        self.assertEqual(self.video.submissions, 0)
        self.assertFalse(self.generation_store.list())

    def test_extraction_failure_resumes_into_generation(self):
        self.provider.responses = [ProviderError("offline interruption")]
        with self.assertRaises(ProviderError):
            self.full_run()
        parent = next(job for job in self.store.list() if "generation_options" in job)
        result = self.invoke("--run", parent["run_id"])
        self.assertEqual(result["status"], "preview_ready")
        self.assertEqual(len(self.generation_store.list()), 1)

    def test_custom_duration_survives_extraction_interruption(self):
        self.provider.responses = [ProviderError("offline interruption")]
        with self.assertRaises(ProviderError):
            self.invoke("--video", str(self.source), "--duration", "6")
        parent = next(job for job in self.store.list() if "generation_options" in job)
        self.config["workflow"]["duration_seconds"] = 18
        result = self.invoke("--run", parent["run_id"])
        inputs = read_json(self.generation_store.path(result["generation_run_id"]) / "input/normalized.json")
        self.assertEqual(inputs["production_constraints"]["duration_seconds"], 6)

    def test_default_duration_is_content_driven_with_sixty_second_cap(self):
        result = self.full_run()
        path = self.generation_store.path(result["generation_run_id"])
        constraints = read_json(path / "input/normalized.json")["production_constraints"]
        self.assertIsNone(constraints["duration_seconds"])
        self.assertEqual(constraints["max_duration_seconds"], 60)
        self.assertEqual(sum(s["duration_seconds"] for s in read_json(path / "story/result.json")["shots"]), 24)

    def test_compact_policy_survives_extraction_interruption_and_exports_timing(self):
        self.config["story"].update(pacing_prompt_version=1, sound_prompt_version=2)
        self.config["workflow"].update(preferred_min_duration_seconds=15, preferred_max_duration_seconds=30)
        self.provider.responses = [ProviderError("interrupted")]
        with self.assertRaises(ProviderError): self.full_run()
        parent = next(job for job in self.store.list() if "generation_options" in job)
        self.config["workflow"]["preferred_max_duration_seconds"] = 45
        self.config["story"]["pacing_prompt_version"] = 0
        result = self.invoke("--run", parent["run_id"])
        path = self.generation_store.path(result["generation_run_id"])
        constraints = read_json(path / "input/normalized.json")["production_constraints"]
        self.assertEqual(constraints["pacing_prompt_version"], 1)
        self.assertEqual(constraints["preferred_max_duration_seconds"], 30)
        self.assertEqual(read_json(Path(result["delivery"]["directory"]) / "时长规划.json")["actual_seconds"], 24)
        counts = (len(self.provider.calls), self.story.calls, self.video.submissions)
        self.invoke("--run", parent["run_id"])
        self.assertEqual((len(self.provider.calls), self.story.calls, self.video.submissions), counts)

    def test_legacy_twelve_second_snapshot_remains_fixed_after_defaults_change(self):
        self.config["workflow"]["duration_seconds"] = 12
        self.provider.responses = [ProviderError("interrupted")]
        with self.assertRaises(ProviderError):
            self.full_run()
        parent = next(job for job in self.store.list() if "generation_options" in job)
        self.config["workflow"]["duration_seconds"] = ""
        result = self.invoke("--run", parent["run_id"])
        constraints = read_json(self.generation_store.path(result["generation_run_id"]) / "input/normalized.json")["production_constraints"]
        self.assertEqual(constraints["duration_seconds"], 12)

    def test_link_tampering_blocks_generation(self):
        self.story.fail_once = True
        with self.assertRaises(ProviderError):
            self.full_run()
        parent = next(job for job in self.store.list() if job.get("generation_run_id"))
        path = self.generation_store.path(parent["generation_run_id"]) / "input/source-extraction.json"
        link = read_json(path)
        link["card_digest"] = "changed"
        write_json(path, link)
        with self.assertRaises(ValidationError):
            self.invoke("--run", parent["run_id"])
        self.assertEqual(self.story.calls, 1)
        self.assertEqual(self.video.submissions, 0)

    def test_video_and_event_inputs_are_mutually_exclusive(self):
        with patch("sys.stderr", new_callable=io.StringIO), self.assertRaises(SystemExit):
            cli.parser().parse_args(["run", "--video", "source.mp4", "--events", "card.json"])

    def test_resume_with_new_video_is_rejected(self):
        with self.assertRaises(ValidationError):
            self.invoke("--run", "existing", "--video", str(self.source))
        self.assertFalse(self.provider.calls)

    def test_bad_production_options_stop_before_extraction(self):
        for flags in (["--duration", "2"], ["--duration", "61"], ["--ratio", "bad"], ["--resolution", "4K"], ["--continuity", "single_take", "--duration", "30"]):
            with self.subTest(flags=flags), self.assertRaises(ValidationError):
                self.invoke("--video", str(self.source), *flags)
        self.assertFalse(self.provider.calls)


if __name__ == "__main__":
    unittest.main()
