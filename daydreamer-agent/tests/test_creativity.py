from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.error import URLError

import test_frame_chain
from test_workflow import WorkflowMedia
from legacy_settings import load_settings
from daydreamer_agent.application.delivery import export_delivery
from daydreamer_agent.domain.errors import ModelOutputError, ProviderError, ValidationError
from daydreamer_agent.providers.qwen import Qwen
from daydreamer_agent.providers.http import JsonHTTP
from daydreamer_agent.storage.files import read_json, write_json
from daydreamer_agent.story.creativity import initialize, previous_work, stage_seed


class CreativeProvider:
    def __init__(self, template):
        self.template = template
        self.calls = []
        self.rejections = 0
        self.fail_stage = None
        self.invalid_audit = False
        self.include_previous = False

    def generate(self, instruction, context, **sampling):
        self.calls.append((deepcopy(context), sampling))
        name = context.get("stage")
        if self.fail_stage is not None and name == self.fail_stage:
            self.fail_stage = None
            raise ProviderError("interruption")
        if context["operation"] == "story_assembly":
            if name == "story_plan":
                return {"status": "ready", "issues": [],
                        "shots": [{"beat_ids": shot["beat_ids"], "duration_seconds": shot["duration_seconds"],
                                   "action_range": shot["start_state"] + "到" + shot["end_state"]} for shot in self.template["shots"]],
                        "audio_plan": {key: self.template["audio_plan"][key] for key in ("music_direction", "sound_motifs")},
                        "assumptions": self.template["assumptions"], "used_memory_ids": [], "world_delta_draft": []}, {}
            shot = deepcopy(self.template["shots"][context["shot_number"] - 1])
            fragment = {"prompt": shot["prompt"], "audio": shot["audio"], "end": shot["continuity"]["end"],
                        "transition_to_next": shot["transition_to_next"]}
            if context["previous_end"] is None:
                fragment["start"] = shot["continuity"]["start"]
            return {"status": "ready", "issues": [], "shot": fragment}, {}
        if context["operation"] == "single_stage" and name in {"theme", "script", "visual_style"}:
            return {"status": "ready", name: deepcopy(self.template[name]), "issues": []}, {}
        if context["operation"] == "similarity_review":
            reject = self.rejections > 0
            self.rejections -= 1
            return {**{key: {"similar": 0 if self.invalid_audit else reject,
                            "reason": "对比幻想变化、因果推进与媒介方法。"}
                       for key in ("theme", "script", "visual_style")},
                    "grounded": True, "grounding_reason": "保留了骑行和按铃动作。"}, {}
        if context["operation"] == "candidates":
            candidates = []
            for index, token in enumerate(("水墨晕染潮汐", "黏土弹跳阶梯", "刺绣旋转星图")):
                value = deepcopy(self.template[name])
                if name == "theme":
                    value.update(core_rule=token + "改变道路重力", development=token + "随车轮缓慢展开")
                elif name == "script":
                    for beat in value:
                        beat.update(visible_change=token + "从车把延伸", end_state=token + "收拢在脚下")
                else:
                    for field in ("medium", "form_and_space", "materials", "motion_character"):
                        value[field] = token
                if self.include_previous and index == 0:
                    value = deepcopy(self.template[name])
                candidates.append({"candidate_id": f"c{index}", name: value})
            return {"status": "ready", "candidates": candidates, "issues": []}, {}
        result = deepcopy(self.template)
        for key in ("theme", "script", "visual_style"):
            result[key] = deepcopy(context["upstream"][key][key])
        return result, {}


class QwenSamplingTests(unittest.TestCase):
    def setUp(self):
        self.http = Mock()
        self.http.request.return_value = {"choices": [{"finish_reason": "stop", "message": {"content": "{}"}}]}
        self.provider = Qwen(SimpleNamespace(openai_base="https://example.invalid", api_key="unused"), transport=self.http)

    def test_seed_is_top_level_api_parameter(self):
        self.provider.generate("test", {}, seed=2147483647, temperature=1.0)
        payload = self.http.request.call_args.args[2]
        self.assertEqual(payload["seed"], 2147483647)
        self.assertEqual(payload["temperature"], 1.0)

    def test_fragment_request_uses_schema_and_specific_output_limit(self):
        schema = {"type": "object", "properties": {}, "required": [], "additionalProperties": False}
        self.provider.generate("单镜头", {"operation": "story_assembly", "stage": "shot"}, seed=123,
                               schema=schema, max_tokens=4096)
        payload = self.http.request.call_args.args[2]
        self.assertEqual(payload["response_format"]["type"], "json_schema")
        self.assertEqual(payload["response_format"]["json_schema"]["schema"], schema)
        self.assertTrue(payload["response_format"]["json_schema"]["strict"])
        self.assertEqual(payload["max_tokens"], 4096)
        self.assertEqual(payload["seed"], 123)

    def test_single_stage_request_limits_output_without_candidates(self):
        self.provider.generate("完整故事模板", {"operation": "single_stage", "stage": "visual_style"}, seed=1)
        instruction = self.http.request.call_args.args[2]["messages"][0]["content"]
        self.assertIn("只返回status、visual_style、issues", instruction)
        self.assertIn("不要生成candidates", instruction)

    def test_output_errors_keep_exact_response_and_distinguish_cause(self):
        for finish, content, kind in (("length", '{"a":', "truncated"),
                                      ("stop", "", "empty_content"),
                                      ("stop", '{"a":', "invalid_json"),
                                      ("stop", "[]", "not_object"),
                                      ("content_filter", "", "filtered")):
            raw = {"choices": [{"finish_reason": finish, "message": {"content": content}}], "usage": {"completion_tokens": 16}}
            self.http.request.return_value = raw
            with self.assertRaises(ModelOutputError) as caught:
                self.provider.generate("test", {}, seed=1)
            self.assertEqual(caught.exception.kind, kind)
            self.assertEqual(caught.exception.response, raw)
            self.assertEqual(caught.exception.repairable, kind != "filtered")

    def test_story_timeout_is_independent_from_video_transport(self):
        credentials = SimpleNamespace(openai_base="https://example.invalid", api_key="unused")
        self.assertEqual(Qwen(credentials).http.timeout, 600)
        self.assertEqual(Qwen(credentials, timeout=900).http.timeout, 900)
        self.assertEqual(JsonHTTP("unused").timeout, 180)

    def test_network_errors_are_distinguishable_without_exposing_secrets(self):
        for exception, expected in ((TimeoutError("secret"), "超时"),
                                    (URLError(TimeoutError("secret")), "超时"),
                                    (URLError("secret"), "连接失败"),
                                    (ValueError("secret"), "JSON")):
            opener = Mock()
            opener.open.side_effect = exception
            with self.assertRaises(ProviderError) as caught:
                JsonHTTP("secret", timeout=600, opener=opener).request("POST", "https://example.invalid", {})
            self.assertIn(expected, str(caught.exception))
            self.assertNotIn("secret", str(caught.exception))
            self.assertTrue(caught.exception.uncertain)
            opener.open.assert_called_once()

    def test_invalid_sampling_fails_before_network(self):
        for value in (-1, 2**31, True, "123"):
            with self.assertRaises(ValidationError):
                self.provider.generate("test", {}, seed=value)
        for value in (float("nan"), 2, -0.1, True):
            with self.assertRaises(ValidationError):
                self.provider.generate("test", {}, seed=1, temperature=value)
        self.http.request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
