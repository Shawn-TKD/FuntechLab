import base64
from copy import deepcopy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, MagicMock, patch

import test_frame_chain
from daydreamer_agent import cli
from daydreamer_agent.application.settings import Credentials, load_settings
from daydreamer_agent.domain.errors import ValidationError, ProviderError
from daydreamer_agent.providers.http import JsonHTTP
from daydreamer_agent.providers.temporary_upload import upload_png
from daydreamer_agent.providers.video_models import H3
from daydreamer_agent.providers.wan import BailianVideo, prepare_submission
from daydreamer_agent.prompts.video_compiler import validate_video_request
from daydreamer_agent.story.timing import fit_plan_timing, validate_timing_constraints
from daydreamer_agent.storage.files import read_json


def request():
    return {"model": H3, "input": {"prompt": "第一人称向前行走"},
            "parameters": {"duration": 6, "resolution": "768P", "ratio": "16:9"}}


class MiniMaxTests(unittest.TestCase):
    def test_model_defaults_and_new_cli_snapshot(self):
        config = load_settings(test_frame_chain.ROOT)
        self.assertEqual(config["video"]["model"], H3)
        self.assertEqual(config["video"]["continuation_model"], H3)
        self.assertEqual(config["workflow"]["resolution"], "768P")
        test_frame_chain.FrameChainTests.setUp(self)
        args = cli.parser().parse_args(["--project", str(test_frame_chain.ROOT), "run", "--events", str(test_frame_chain.ROOT / "examples/events.json")])
        # The legacy sample explicitly selects 480P; a new request selects 768P.
        args.resolution = "768P"
        run = cli.new_run(args, config, self.store)
        constraints = read_json(self.store.path(run) / "input/normalized.json")["production_constraints"]
        self.assertEqual(constraints["video_model"], H3)
        self.assertEqual(constraints["max_duration_seconds"], 60)

    def test_model_specific_bounds_before_submission(self):
        validate_video_request(request())
        for duration, resolution in [(3, "768P"), (16, "768P"), (6, "720P"), (True, "768P")]:
            body = request(); body["parameters"].update(duration=duration, resolution=resolution)
            with self.assertRaises(ValidationError): validate_video_request(body)
        body = request(); body["input"]["prompt"] = "字" * 7001
        with self.assertRaises(ValidationError): validate_video_request(body)

    def test_plan_allocates_four_second_floor_and_cap(self):
        constraints = {"video_model": H3, "max_duration_seconds": 60}
        plan = {"shots": [{"duration_seconds": n} for n in [3, 3, 8]]}
        result = fit_plan_timing(plan, constraints)
        self.assertEqual([s["duration_seconds"] for s in result["shots"]], [4, 4, 8])
        self.assertEqual(plan["shots"][0]["duration_seconds"], 3)
        with self.assertRaises(ValidationError): validate_timing_constraints({**constraints, "duration_seconds": 3})
        with self.assertRaises(ValidationError): fit_plan_timing({"shots": [{"duration_seconds": 4}] * 16}, constraints)
        result = fit_plan_timing({"shots": [{"duration_seconds": 15} for _ in range(8)]}, constraints)
        self.assertEqual(sum(s["duration_seconds"] for s in result["shots"]), 60)

    def test_continuation_uses_actual_tail_and_h3_payload(self):
        test_frame_chain.FrameChainTests.setUp(self)
        config = load_settings(test_frame_chain.ROOT)
        inputs = read_json(self.path / "input/normalized.json")
        inputs["production_constraints"].update(video_model=H3, resolution="768P", duration_seconds=8)
        story = read_json(self.path / "story/result.json")
        for shot in story["shots"]: shot["duration_seconds"] = 4
        run = self.store.create(inputs, config)
        self.pipeline.finish_story(run, story)
        self.pipeline.render(run)
        first, second = self.video.payloads
        self.assertEqual(first["model"], H3)
        self.assertNotIn("media", first["input"])
        self.assertNotIn("audio", first["parameters"])
        self.assertNotIn("prompt_extend", first["parameters"])
        self.assertEqual(second["parameters"]["ratio"], "adaptive")
        previous = self.store.shot(run, story["shots"][0]["shot_id"])
        self.assertEqual(base64.b64decode(second["input"]["media"][0]["url"].split(",")[1]),
                         (self.store.path(run) / previous["tail_frame"]).read_bytes())

    def test_submit_uploads_before_post_without_mutating_checkpoint(self):
        http = Mock(); http.request.return_value = {"output": {"task_id": "task-h3"}}
        provider = BailianVideo(Credentials("test", "https://dashscope.aliyuncs.com/compatible-mode/v1", "https://dashscope.aliyuncs.com/api/v1"), http)
        body = request(); body["input"]["media"] = [{"type": "first_frame", "url": "data:image/png;base64," + base64.b64encode(b"test-png").decode()}]
        body["parameters"]["ratio"] = "adaptive"
        original = deepcopy(body)
        with patch("daydreamer_agent.providers.wan.upload_png", return_value="oss://temporary/frame.png") as upload:
            self.assertEqual(provider.submit(body), "task-h3")
        self.assertEqual(upload.call_args.args[-1], b"test-png")
        self.assertEqual(http.request.call_args.args[2]["input"]["media"][0]["url"], "oss://temporary/frame.png")
        self.assertEqual(body, original)
        with patch("daydreamer_agent.providers.wan.upload_png", side_effect=ProviderError("upload failed")):
            http.reset_mock()
            with self.assertRaises(ProviderError): provider.submit(body)
            http.request.assert_not_called()

    def test_oss_resolver_header(self):
        response = Mock(); response.read.return_value = b'{}'
        opener = MagicMock(); opener.open.return_value.__enter__.return_value = response
        body = request(); body["input"]["media"] = [{"url": "oss://temporary/frame.png"}]
        JsonHTTP("key", opener=opener).request("POST", "https://dashscope.aliyuncs.com/api/v1/test", body, async_video=True)
        headers = dict((k.lower(), v) for k, v in opener.open.call_args.args[0].header_items())
        self.assertEqual(headers["x-dashscope-ossresourceresolve"], "enable")

    def test_upload_does_not_send_api_key_and_rejects_foreign_host(self):
        policy = dict(upload_host="https://bucket.oss-cn-beijing.aliyuncs.com", upload_dir="temporary", oss_access_key_id="oss-id", signature="sig", policy="policy", x_oss_object_acl="private", x_oss_forbid_overwrite="true")
        http = Mock(); http.request.return_value = {"data": policy}
        opener = MagicMock(); opener.open.return_value.__enter__.return_value.status = 200
        self.assertTrue(upload_png(http, "https://dashscope.aliyuncs.com/api/v1", H3, b"png", opener=opener).startswith("oss://temporary/"))
        req = opener.open.call_args.args[0]
        self.assertIsNone(req.get_header("Authorization"))
        self.assertIn(b'name="file"', req.data)
        policy["upload_host"] = "https://attacker.example/upload"
        opener.reset_mock()
        with self.assertRaises(ProviderError): upload_png(http, "https://dashscope.aliyuncs.com/api/v1", H3, b"png", opener=opener)
        opener.open.assert_not_called()
