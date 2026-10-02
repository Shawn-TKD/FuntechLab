from legacy_settings import load_settings
from copy import deepcopy
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from daydreamer_agent.application.pipeline import Pipeline
from daydreamer_agent.application.settings import Credentials, validate_endpoint
from daydreamer_agent.domain.errors import BusyError, ProviderError, ValidationError
from daydreamer_agent.domain.events import normalize
from daydreamer_agent.prompts.video_compiler import compile_shot
from daydreamer_agent.providers.http import JsonHTTP, download
from daydreamer_agent.providers.wan import WanVideo
from daydreamer_agent.providers.qwen import Qwen
from daydreamer_agent.storage.files import digest, read_json, write_json
from daydreamer_agent.storage.jobs_sqlite import JobStore
from daydreamer_agent.story.generator import generate_story, load_skill
from daydreamer_agent.story.validation import validate_story


class FakeMedia:
    def require_tools(self):
        pass

    def check_clip(self, path, parameters, **kwargs):
        return {"duration": parameters["duration"]}


class FakeVideo:
    def __init__(self, results=None, error=None):
        self.submissions = 0
        self.queries = []
        self.results = list(results or [])
        self.error = error

    def submit(self, request):
        self.submissions += 1
        if self.error:
            raise self.error
        return "task-" + str(self.submissions)

    def query(self, task_id):
        self.queries.append(task_id)
        return self.results.pop(0) if self.results else {"task_status": "SUCCEEDED", "video_url": "https://test.oss-cn-beijing.aliyuncs.com/clip.mp4"}

    def download(self, url, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"offline-test-video")


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        self.inputs = normalize(read_json(ROOT / "examples/events.json"))
        self.story = read_json(ROOT / "examples/story.json")
        self.config = load_settings(ROOT)
        self.store = JobStore(self.path / "runs", self.path / "jobs.sqlite3")
        self.run = self.store.create(self.inputs, self.config)
        self.video = FakeVideo()
        self.pipeline = Pipeline(self.store, video_provider=self.video, media=FakeMedia(), progress=lambda _: None)
        self.pipeline.finish_story(self.run, self.story)

    def test_original_input_is_not_mutated(self):
        raw = [{"summary": "走路"}]
        result = normalize(raw)
        self.assertNotIn("event_id", raw[0])
        self.assertEqual(result["event_cards"][0]["id_basis"], "local")

    def test_duplicate_events_rejected(self):
        with self.assertRaises(ValidationError):
            normalize([{"event_id": "same", "summary": "a"}, {"event_id": "same", "summary": "b"}])

    def test_extra_prompt_field_rejected(self):
        self.story["shots"][0]["prompt"]["音效"] = "铃声"
        with self.assertRaises(ValidationError):
            validate_story(self.story, self.inputs)

    def test_invented_source_rejected(self):
        self.story["shots"][0]["source_refs"] = ["invented.jpg"]
        with self.assertRaises(ValidationError):
            validate_story(self.story, self.inputs)

    def test_duration_mismatch_rejected(self):
        self.story["shots"][0]["duration_seconds"] = 4
        with self.assertRaises(ValidationError):
            validate_story(self.story, self.inputs)

    def test_short_shots_rejected_even_when_total_matches(self):
        self.story["shots"][0]["duration_seconds"] = 2
        self.story["shots"][1]["duration_seconds"] = 4
        with self.assertRaises(ValidationError):
            validate_story(self.story, self.inputs)

    def test_memory_version_mismatch_rejected(self):
        self.story["memory_basis"]["version"] = "invented"
        with self.assertRaises(ValidationError):
            validate_story(self.story, self.inputs)

    def test_path_traversal_shot_rejected(self):
        self.story["shots"][0]["shot_id"] = "../private"
        with self.assertRaises(ValidationError):
            validate_story(self.story, self.inputs)

    def test_prompt_carries_theme_style_and_states(self):
        for shot in self.story["shots"]:
            prompt = compile_shot(self.story, shot, self.inputs["production_constraints"], "wan3.0-video-prime")["request"]["input"]["prompt"]
            for value in (self.story["theme"]["core_rule"], self.story["visual_style"]["medium"], shot["start_state"], shot["end_state"]):
                self.assertIn(value, prompt)

    def test_overlong_prompt_not_truncated(self):
        self.story["shots"][0]["prompt"]["画面内容"] = "第一人称" + "画" * 20000
        with self.assertRaises(ValidationError):
            compile_shot(self.story, self.story["shots"][0], {}, "model")

    def test_video_prompt_carries_goal_and_only_current_plot_with_previous_progress(self):
        self.story["theme"]["action_goal"] = "持续追赶携包裹的信使"
        self.story["script"][0]["first_person_action"] = "已完成的前段动作_不能重演"
        self.story["script"][1]["first_person_action"] = "本段完成右转并重新对准目标"
        self.story["shots"][0]["end_state"] = "已绕过石墩，正在开始右转"
        self.story["script"].append({
            "beat_id": "future", "first_person_action": "未来桥头事件_尚未发生",
            "visible_change": "未来守卫出现", "end_state": "未来状态",
        })
        shot = self.story["shots"][1]
        prompt = compile_shot(self.story, shot, {}, "wan3.0-video-prime")["request"]["input"]["prompt"]
        for text in (self.story["theme"]["action_goal"], self.story["shots"][0]["end_state"],
                     self.story["script"][1]["first_person_action"], shot["end_state"]):
            self.assertIn(text, prompt)
        self.assertNotIn("已完成的前段动作_不能重演", prompt)
        self.assertNotIn("未来桥头事件_尚未发生", prompt)
        self.assertNotIn("未来守卫出现", prompt)

    def test_shared_beat_is_scoped_to_current_shot_boundaries(self):
        self.story["shots"][1]["beat_ids"] = self.story["shots"][0]["beat_ids"]
        self.story["shots"][1]["start_state"] = "已经进入右转弯道"
        self.story["shots"][1]["end_state"] = "完成转弯并摆正车把"
        prompt = compile_shot(self.story, self.story["shots"][1], {}, "wan3.0-video-prime")["request"]["input"]["prompt"]
        self.assertIn(self.story["script"][0]["first_person_action"], prompt)
        self.assertIn("已经进入右转弯道", prompt)
        self.assertIn("完成转弯并摆正车把", prompt)
        self.assertIn("同一剧情段落跨镜时只执行本镜范围", prompt)

    def test_success_is_reused(self):
        self.assertEqual(self.pipeline.render(self.run)["status"], "awaiting_audio")
        self.pipeline.render(self.run)
        self.assertEqual(self.video.submissions, 2)

    def test_running_resumes_same_task(self):
        self.video.results = [{"task_status": "RUNNING"}]
        self.pipeline.render(self.run)
        self.assertEqual(self.video.submissions, 1)
        self.pipeline.render(self.run)
        self.assertEqual(self.video.submissions, 2)
        self.assertEqual(self.video.queries[:2], ["task-1", "task-1"])

    def test_timeout_never_resubmits(self):
        self.video.error = ProviderError("timeout", uncertain=True)
        with self.assertRaises(ProviderError):
            self.pipeline.render(self.run)
        self.assertEqual(self.store.load(self.run)["status"], "submission_unknown")
        self.video.error = None
        self.pipeline.render(self.run)
        self.assertEqual(self.video.submissions, 1)
        with self.assertRaises(ValidationError):
            self.pipeline.retry_shot(self.run, "shot-01")

    def test_crash_between_submission_and_persistence_is_unknown(self):
        self.store.save_shot(self.run, {"shot_id": "shot-01", "status": "submitting", "attempt": 1})
        self.assertEqual(self.pipeline.render(self.run)["status"], "submission_unknown")
        self.assertEqual(self.video.submissions, 0)

    def test_attach_task_recovers_without_new_submission(self):
        self.store.save_shot(self.run, {"shot_id": "shot-01", "status": "submission_unknown", "attempt": 1})
        self.pipeline.attach_task(self.run, "shot-01", "existing-task")
        self.pipeline.render(self.run)
        self.assertEqual(self.video.queries[0], "existing-task")
        self.assertEqual(self.video.submissions, 1)

    def test_expired_task_not_recreated(self):
        self.video.results = [{"task_status": "UNKNOWN"}]
        self.pipeline.render(self.run)
        self.pipeline.render(self.run)
        self.assertEqual(self.video.submissions, 1)

    def test_failed_shot_explicit_retry_preserves_attempt(self):
        self.video.results = [{"task_status": "FAILED"}]
        self.pipeline.render(self.run)
        self.pipeline.retry_shot(self.run, "shot-01")
        self.pipeline.render(self.run)
        self.assertEqual(self.store.shot(self.run, "shot-01")["attempt"], 2)
        old = self.store.path(self.run) / "shots/shot-01/attempts/1/task.json"
        self.assertEqual(read_json(old)["status"], "failed")

    def test_modified_story_does_not_mix_old_clips(self):
        self.story["theme"]["title"] = "changed"
        write_json(self.store.path(self.run) / "story/result.json", self.story)
        with self.assertRaises(ValidationError):
            self.pipeline.render(self.run)
        self.assertEqual(self.video.submissions, 0)

    def test_modified_request_rejected_before_any_spend(self):
        path = self.store.path(self.run) / "shots/shot-02/video_request.json"
        request = read_json(path)
        request["request"]["parameters"]["duration"] = 4
        write_json(path, request)
        with self.assertRaises(ValidationError):
            self.pipeline.render(self.run)
        self.assertEqual(self.video.submissions, 0)

    def test_missing_clip_redownloads_original(self):
        self.pipeline.render(self.run)
        state = self.store.shot(self.run, "shot-01")
        (self.store.path(self.run) / state["clip"]).unlink()
        self.pipeline.render(self.run)
        self.assertEqual(self.video.submissions, 2)

    def test_missing_audio_not_completed(self):
        self.pipeline.render(self.run)
        self.assertEqual(self.pipeline.compose(self.run)["status"], "awaiting_audio")

    def test_run_lock_rejects_second_writer(self):
        with self.store.lock(self.run):
            with self.assertRaises(BusyError):
                with self.store.lock(self.run):
                    pass

    def test_story_resume_reuses_checkpoints(self):
        class StoryProvider:
            count = 0
            fail_once = True

            def generate(inner, instruction, context):
                inner.count += 1
                upstream = context["upstream"]
                if len(upstream) == 2 and inner.fail_once:
                    inner.fail_once = False
                    raise ProviderError("offline failure")
                names = ["theme", "script", "visual_style"]
                if len(upstream) < 3:
                    name = names[len(upstream)]
                    return {"status": "ready", name: self.story[name], "issues": []}, {}
                return deepcopy(self.story), {}
        provider = StoryProvider()
        skill = load_skill(ROOT / "resources/daydreamer-style-transfer")
        path = self.store.path(self.run)
        with self.assertRaises(ProviderError):
            generate_story(path, self.inputs, skill, provider)
        result = generate_story(path, self.inputs, skill, provider)
        self.assertEqual(result["status"], "ready")
        self.assertEqual(provider.count, 5)  # Four generation stages plus the interrupted request; no review call.
        self.assertEqual(result, self.story)
        self.assertFalse((path / "story/review.json").exists())


class HTTPTests(unittest.TestCase):
    def test_untrusted_endpoint_rejected(self):
        for url in ("https://evil.example/api/v1", "https://dashscope.aliyuncs.com.evil.example/api/v1", "http://dashscope.aliyuncs.com/api/v1"):
            with self.assertRaises(ValidationError):
                validate_endpoint(url)

    def test_submit_post_never_auto_retries(self):
        class Opener:
            calls = 0
            def open(inner, *args, **kwargs):
                inner.calls += 1
                raise URLError("secret-key must not appear")
        opener = Opener()
        with self.assertRaises(ProviderError) as caught:
            JsonHTTP("secret-key", opener=opener).request("POST", "https://dashscope.aliyuncs.com/api/v1", {})
        self.assertTrue(caught.exception.uncertain)
        self.assertNotIn("secret-key", str(caught.exception))
        self.assertEqual(opener.calls, 1)

    def test_authentication_error_not_retried(self):
        class Opener:
            calls = 0
            def open(inner, *args, **kwargs):
                inner.calls += 1
                raise HTTPError("url", 401, "unauthorized", {}, io.BytesIO(b'{"code":"InvalidApiKey"}'))
        opener = Opener()
        with self.assertRaises(ProviderError) as caught:
            JsonHTTP("secret", opener=opener).request("GET", "https://dashscope.aliyuncs.com/api/v1")
        self.assertFalse(caught.exception.retryable)
        self.assertEqual(opener.calls, 1)

    def test_get_retry_is_bounded(self):
        class Opener:
            calls = 0
            def open(inner, *args, **kwargs):
                inner.calls += 1
                raise URLError("offline")
        opener = Opener()
        with patch("daydreamer_agent.providers.http.time.sleep"), self.assertRaises(ProviderError):
            JsonHTTP("secret", opener=opener).request("GET", "https://dashscope.aliyuncs.com/api/v1")
        self.assertEqual(opener.calls, 3)

    def test_download_never_attaches_model_authorization(self):
        class Opener:
            def open(inner, request, **kwargs):
                self.assertNotIn("Authorization", request.headers)
                return io.BytesIO(b"clip")
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "clip.mp4"
            download("https://bucket.oss-cn-beijing.aliyuncs.com/test.mp4", target, opener=Opener())
            self.assertEqual(target.read_bytes(), b"clip")

    def test_video_provider_uses_async_header_and_task_path(self):
        class Transport:
            calls = []
            def request(inner, *args, **kwargs):
                inner.calls.append((args, kwargs))
                return {"output": {"task_id": "id-1", "task_status": "PENDING"}}
        transport = Transport()
        credentials = Credentials("secret", "https://dashscope.aliyuncs.com/compatible-mode/v1", "https://dashscope.aliyuncs.com/api/v1")
        provider = WanVideo(credentials, transport)
        self.assertEqual(provider.submit({"model": "wan3.0-video-prime"}), "id-1")
        provider.query("id-1")
        self.assertTrue(transport.calls[0][1]["async_video"])
        self.assertTrue(transport.calls[1][0][1].endswith("/tasks/id-1"))


if __name__ == "__main__":
    unittest.main()
