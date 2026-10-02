from copy import deepcopy
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import test_frame_chain
from daydreamer_agent import cli
from daydreamer_agent.application.delivery import export_delivery
from legacy_settings import load_settings
from daydreamer_agent.domain.errors import ValidationError
from daydreamer_agent.storage.files import file_hash, read_json, write_json
from daydreamer_agent.storage.jobs_sqlite import JobStore

ROOT = test_frame_chain.ROOT


class WorkflowMedia(test_frame_chain.ChainMedia):
    def compose(self, path, clips, tracks, parameters, **kwargs):
        target = path / "composition/test-preview.mp4"
        target.write_bytes(b"".join(clip.read_bytes() for clip, _ in clips))
        return target


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        test_frame_chain.FrameChainTests.setUp(self)
        self.pipeline.media = WorkflowMedia()

    def test_all_stages_and_export_then_resume_without_model_calls(self):
        story = read_json(self.path / "story/result.json")
        inputs = read_json(self.path / "input/normalized.json")
        run_id = self.store.create(inputs, load_settings(ROOT))
        path = self.store.path(run_id)
        write_json(path / "input/skill.json", {"test": "offline fixture"})
        class Story:
            count = 0
            def generate(inner, instruction, context):
                inner.count += 1
                index = len(context["upstream"])
                if index < 3:
                    name = ("theme", "script", "visual_style")[index]
                    return {"status": "ready", name: story[name], "issues": []}, {}
                return deepcopy(story), {}
        provider = Story()
        self.pipeline.story_provider = provider
        result = self.pipeline.run_full(run_id, wait_seconds=0)
        self.assertEqual(result["status"], "preview_ready")
        self.assertEqual((provider.count, self.video.submissions), (4, 2))
        exported = export_delivery(path, path.parent / "delivery")
        self.assertEqual(file_hash(exported["video"]), file_hash(result["preview"]))
        self.assertTrue(Path(exported["storyboard"]).is_file())
        self.pipeline.run_full(run_id, wait_seconds=0)
        self.assertEqual((provider.count, self.video.submissions), (4, 2))
        record = read_json(Path(exported["directory"]) / "本轮运行记录.json")
        self.assertFalse(record["continuity_verified"])

    def test_incomplete_video_stops_before_composition_and_resumes(self):
        self.video.results = [{"task_status": "RUNNING"}]
        with patch.object(self.pipeline, "compose", wraps=self.pipeline.compose) as compose:
            result = self.pipeline.run_full(self.run, wait_seconds=0)
            self.assertNotEqual(result["status"], "preview_ready")
            compose.assert_not_called()
            self.assertEqual(self.pipeline.run_full(self.run, wait_seconds=0)["status"], "preview_ready")
            self.assertEqual(self.video.submissions, 2)

    def test_unresolved_story_never_submits_video(self):
        self.store.load = Mock(return_value={"status": "created"})
        self.pipeline.plan = Mock(return_value={"status": "needs_resolution"})
        self.assertEqual(self.pipeline.run_full(self.run)["status"], "needs_resolution")
        self.assertEqual(self.video.submissions, 0)

    def test_delivery_rejects_changed_or_stale_video(self):
        result = self.pipeline.run_full(self.run, wait_seconds=0)
        Path(result["preview"]).write_bytes(b"changed")
        with self.assertRaises(ValidationError):
            export_delivery(self.path, self.path.parent / "delivery")

    def test_completed_resume_skips_providers_and_accepts_explicit_audio(self):
        self.store.status(self.run, "completed")
        self.pipeline.render = Mock()
        self.pipeline.compose = Mock(return_value={"status": "completed"})
        self.assertEqual(self.pipeline.run_full(self.run)["status"], "completed")
        # Completed jobs still verify the cached delivery without invoking models.
        self.pipeline.compose.assert_called_once_with(self.run, None, preview=False)
        self.pipeline.compose.reset_mock()
        manifest = self.path / "audio.json"
        write_json(manifest, {})
        self.pipeline.run_full(self.run, audio_manifest=manifest)
        self.pipeline.compose.assert_called_once_with(self.run, manifest, preview=False)
        self.pipeline.render.assert_not_called()


class WorkflowCliTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.config = load_settings(ROOT)
        self.store = JobStore(self.root / "runs", self.root / "jobs.sqlite3")
        self.events = self.root / "事件 卡.json"

    def create(self, card, extra=()):
        write_json(self.events, card)
        args = cli.parser().parse_args(["--project", str(self.root), "run", "--events", str(self.events), *extra])
        with patch.object(cli, "load_skill", return_value={}), patch("sys.stdout", new_callable=io.StringIO):
            run_id = cli.new_run(args, self.config, self.store)
        creativity = read_json(self.store.path(run_id) / "input/creativity.json")
        self.assertIs(type(creativity["seed"]), int)
        self.assertTrue(creativity["policy"]["enabled"])
        return read_json(self.store.path(run_id) / "input/normalized.json")["production_constraints"]

    def test_json_without_parameters_gets_workflow_defaults(self):
        constraints = self.create([{"summary": "骑自行车"}])
        self.assertEqual((constraints["duration_seconds"], constraints["aspect_ratio"], constraints["resolution"]), (None, "16:9", "720P"))
        self.assertEqual(constraints["max_duration_seconds"], 60)
        self.assertEqual(constraints["continuity_mode"], "frame_chain")

    def test_card_parameters_and_explicit_overrides_win(self):
        card = read_json(ROOT / "examples/events.json")
        self.assertEqual(self.create(card)["duration_seconds"], 6)
        constraints = self.create(card, ["--duration", "9", "--resolution", "1080P"])
        self.assertEqual((constraints["duration_seconds"], constraints["resolution"]), (9, "1080P"))

    def test_conflicting_resume_input_fails_before_credentials(self):
        args = cli.parser().parse_args(["--project", str(self.root), "run", "--run", "existing", "--events", str(self.events)])
        with patch.object(cli, "load_settings", return_value=self.config), patch.object(cli.Credentials, "load") as credentials:
            with self.assertRaises(ValidationError):
                cli.run_command(args)
            credentials.assert_not_called()

    def test_configured_csv_is_used_and_incomplete_run_returns_nonzero(self):
        credential_path = self.root / "test-credentials.csv"
        credential_path.write_text("offline placeholder", encoding="utf-8")
        self.config["credentials"]["csv_path"] = str(credential_path)
        self.config["storage"]["run_directory"] = "runs"
        self.config["storage"]["task_database"] = "jobs.sqlite3"
        run_id = self.store.create(read_json(ROOT / "examples/events.json"), self.config)
        with patch.object(cli, "load_settings", return_value=self.config), patch.object(cli, "Media"), \
             patch.object(cli, "Qwen"), patch.object(cli, "BailianVideo"), \
             patch.object(cli.Credentials, "load") as credentials, \
             patch.object(cli.Pipeline, "run_full", return_value={"status": "needs_resolution"}), \
             patch("sys.stdout", new_callable=io.StringIO):
            code = cli.main(["--project", str(self.root), "run", "--run", run_id])
        self.assertEqual(code, 2)
        credentials.assert_called_once_with(self.root.resolve(), credential_path)


if __name__ == "__main__":
    unittest.main()
