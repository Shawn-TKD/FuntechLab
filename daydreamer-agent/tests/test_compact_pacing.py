"""Compact pacing and restrained ambience, with offline providers only."""
from copy import deepcopy
from pathlib import Path
import unittest

import test_frame_chain
from test_creativity import CreativeProvider
from test_workflow import WorkflowMedia
from daydreamer_agent import cli
from daydreamer_agent.application.delivery import export_delivery
from daydreamer_agent.application.settings import load_settings
from daydreamer_agent.domain.errors import ValidationError, ProviderError
from daydreamer_agent.providers.video_models import H3
from daydreamer_agent.storage.files import read_json, write_json
from daydreamer_agent.story.creativity import initialize
from daydreamer_agent.story.generator import generate_story
from daydreamer_agent.story.pacing import freeze_policy, configured_policy, pacing_instruction
from daydreamer_agent.story.sound import SOUND_DIRECTION, LOW_NOISE_DIRECTION, LOW_NOISE_EXECUTION, sound_direction
from daydreamer_agent.story.timing import fit_plan_timing

ROOT = test_frame_chain.ROOT


def compact_constraints(**overrides):
    result = {"duration_seconds": None, "video_model": H3, "continuity_mode": "frame_chain"}
    freeze_policy(result, load_settings(ROOT))
    result.update(overrides)
    return result


def plan(durations, extended=False):
    return {"shots": [{"duration_seconds": d, "beat_ids": [f"b{i}"]} for i, d in enumerate(durations)],
            "timing_reason": {"reason": "依次完成接近、绕行和反馈，保留必要动作时间。",
                              "extension_reason": "绕行后需要呈现目标的反应和继续接近，省略会丢失因果。" if extended else "",
                              "extension_beat_ids": ["b0"] if extended else []}}


class CompactTimingTests(unittest.TestCase):
    def test_preferred_range_does_not_pad_short_or_normal_plans(self):
        for values in ([6, 6], [9, 9], [12, 12], [14, 15]):
            with self.subTest(values=values):
                raw = plan(values)
                self.assertEqual(fit_plan_timing(raw, compact_constraints()), raw)

    def test_extension_requires_reason_and_valid_beat_references(self):
        raw = plan([12, 12, 12], extended=True)
        self.assertEqual(fit_plan_timing(raw, compact_constraints()), raw)
        for reason in (None, {"reason": "动作", "extension_reason": "", "extension_beat_ids": []},
                       {"reason": "动作", "extension_reason": "原因", "extension_beat_ids": ["missing"]},
                       {"reason": "动作", "extension_reason": "原因", "extension_beat_ids": ["b0", "b0"]}):
            with self.subTest(reason=reason), self.assertRaises(ValidationError):
                fit_plan_timing({**raw, "timing_reason": reason}, compact_constraints())

    def test_over_cap_is_not_scaled_down(self):
        raw = plan([15] * 5, extended=True)
        with self.assertRaisesRegex(ValidationError, "重新.*规划"):
            fit_plan_timing(raw, compact_constraints())
        self.assertEqual(sum(s["duration_seconds"] for s in raw["shots"]), 75)
        self.assertEqual(fit_plan_timing(plan([15] * 4, True), compact_constraints())["shots"], plan([15] * 4)["shots"])

    def test_explicit_total_overrides_preferred_range_and_still_fits_integers(self):
        for target, values in ((12, [6, 6]), (40, [13, 13, 13])):
            result = fit_plan_timing(plan(values), compact_constraints(duration_seconds=target))
            self.assertEqual(sum(s["duration_seconds"] for s in result["shots"]), target)
        with self.assertRaises(ValidationError):
            fit_plan_timing(plan([15] * 5), compact_constraints(duration_seconds=61))

    def test_new_policy_rejects_missing_times_and_respects_model_and_shot_limits(self):
        for value in (None, 3, 16, True, 4.5, "6"):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                fit_plan_timing(plan([value]), compact_constraints())
        for constraints in (compact_constraints(continuity_mode="single_take"), compact_constraints(shot_limit=1)):
            with self.assertRaises(ValidationError):
                fit_plan_timing(plan([8, 8]), constraints)
        self.assertEqual(fit_plan_timing(plan([12]), compact_constraints(continuity_mode="single_take")), plan([12]))

    def test_legacy_allocation_and_null_are_preserved(self):
        legacy = {"max_duration_seconds": 60}
        result = fit_plan_timing(plan([15] * 5), legacy)
        self.assertEqual(sum(s["duration_seconds"] for s in result["shots"]), 60)
        self.assertEqual(fit_plan_timing(plan([None]), legacy)["shots"][0]["duration_seconds"], 6)

    def test_configuration_and_explicit_budget_text(self):
        for changes in ({"preferred_min_duration_seconds": 31}, {"preferred_max_duration_seconds": 61},
                        {"preferred_min_duration_seconds": True}, {"max_duration_seconds": None}):
            config = load_settings(ROOT)
            config["workflow"].update(changes)
            with self.assertRaises(ValidationError): configured_policy(config)
        for version in (True, "1", 2):
            config = load_settings(ROOT); config["story"]["pacing_prompt_version"] = version
            with self.assertRaises(ValidationError): configured_policy(config)
        self.assertIn("明确指定总时长40秒", pacing_instruction(compact_constraints(duration_seconds=40)))
        self.assertIn("最多15秒", pacing_instruction(compact_constraints(continuity_mode="single_take")))
        self.assertEqual(pacing_instruction({}), "")


class CompactProvider(CreativeProvider):
    def __init__(self, template):
        super().__init__(template)
        self.instructions = []
        self.omit_extension_once = False

    def generate(self, instruction, context, **sampling):
        self.instructions.append((context.get("stage"), instruction))
        output, usage = super().generate(instruction, context, **sampling)
        if context.get("stage") == "story_plan" and context["production_constraints"].get("pacing_prompt_version"):
            total = sum(s["duration_seconds"] for s in output["shots"])
            extend = context["production_constraints"].get("duration_seconds") is None and total > 30
            omit = self.omit_extension_once and "repair" not in context
            output["timing_reason"] = {"reason": "保留通过障碍与目标反应的时间。",
                "extension_reason": "转弯后需要显示目标反应，无法同时省略空间过渡。" if extend and not omit else "",
                "extension_beat_ids": output["shots"][-1]["beat_ids"] if extend and not omit else []}
        return output, usage


class CompactPipelineTests(unittest.TestCase):
    def setUp(self):
        test_frame_chain.FrameChainTests.setUp(self)
        self.template = read_json(self.path / "story/result.json")
        self.inputs = read_json(self.path / "input/normalized.json")
        self.inputs["production_constraints"].update(compact_constraints(), resolution="768P")
        self.config = load_settings(ROOT)
        for shot in self.template["shots"]:
            shot["duration_seconds"] = 12
        self.run = self.store.create(self.inputs, self.config)
        self.path = self.store.path(self.run)
        write_json(self.path / "input/skill.json", {})
        initialize(self.store, self.run, self.config)
        self.provider = CompactProvider(self.template)
        self.pipeline.story_provider = self.provider
        self.pipeline.media = WorkflowMedia()

    def test_new_pipeline_freezes_budget_generates_without_extra_stages_and_exports_reason(self):
        result = self.pipeline.run_full(self.run, wait_seconds=0)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(self.provider.calls), 6)
        for stage, instruction in self.provider.instructions:
            if stage in ("theme", "script", "story_plan", "shot"):
                self.assertIn("15–30秒", instruction)
            if stage in ("story_plan", "shot"):
                self.assertIn(LOW_NOISE_DIRECTION, instruction)
        plan_call = next(s for ctx, s in self.provider.calls if ctx["stage"] == "story_plan")
        schema = plan_call["schema"]
        self.assertIn("timing_reason", schema["required"])
        self.assertEqual(schema["properties"]["shots"]["items"]["properties"]["duration_seconds"], {"type": "integer"})
        story = read_json(self.path / "story/result.json")
        self.assertNotIn("timing_reason", story)
        self.assertEqual(sum(s["duration_seconds"] for s in story["shots"]), 24)
        for shot in story["shots"]:
            req = read_json(self.path / "shots" / shot["shot_id"] / "video_request.json")
            self.assertEqual(req["template_version"], "2.3")
            self.assertIn("【本镜节奏】", req["request"]["input"]["prompt"])
            self.assertIn(LOW_NOISE_EXECUTION, req["request"]["input"]["prompt"])
        exported = export_delivery(self.path, self.path / "delivery", preview=False)
        record = read_json(Path(exported["directory"]) / "本轮运行记录.json")
        self.assertEqual(record["timing"]["actual_seconds"], 24)
        self.assertTrue((Path(exported["directory"]) / "时长规划.json").is_file())
        self.assertIn("时长依据", Path(exported["storyboard"]).read_text(encoding="utf-8"))
        self.pipeline.run_full(self.run, wait_seconds=0)
        self.assertEqual(len(self.provider.calls), 6)
        self.assertEqual(self.video.submissions, 2)

    def test_extension_uses_existing_bounded_repair_not_extra_review(self):
        last = deepcopy(self.template["shots"][-1])
        self.template["shots"][-1]["transition_to_next"] = "继续接近目标。"
        self.template["shots"].append(last)
        self.provider.omit_extension_once = True
        self.pipeline.plan(self.run)
        plans = [c for c, _ in self.provider.calls if c["stage"] == "story_plan"]
        self.assertEqual(len(plans), 2)
        self.assertEqual(plans[1]["repair"]["field"], "timing_reason")
        self.assertEqual(sum(c["stage"] == "theme" for c, _ in self.provider.calls), 1)
        timing = read_json(self.path / "story/timing.json")
        self.assertEqual(timing["actual_seconds"], 36)
        self.assertTrue(timing["timing_reason"]["extension_reason"])
        self.assertNotIn("review", [c["stage"] for c, _ in self.provider.calls])
        counts = len(self.provider.calls)
        self.pipeline.plan(self.run)
        self.assertEqual(len(self.provider.calls), counts)

    def test_resume_after_interruption_reuses_completed_stages_with_frozen_policy(self):
        self.provider.fail_stage = "shot"
        with self.assertRaises(ProviderError): self.pipeline.plan(self.run)
        count = len(self.provider.calls)
        self.config["workflow"]["preferred_max_duration_seconds"] = 45
        self.config["story"]["sound_prompt_version"] = 0
        self.pipeline.plan(self.run)
        self.assertEqual(len(self.provider.calls) - count, 2)
        self.assertIn("15–30秒", self.provider.instructions[-1][1])
        self.assertIn(LOW_NOISE_DIRECTION, self.provider.instructions[-1][1])

    def test_excess_duration_exhausts_bounded_repair_before_video_submission(self):
        for shot in self.template["shots"]:
            shot["duration_seconds"] = 15
            shot["transition_to_next"] = "继续前进。"
        self.template["shots"].extend(deepcopy(self.template["shots"][-1]) for _ in range(3))
        self.template["shots"][-1]["transition_to_next"] = None
        config = read_json(self.path / "input/config.json")
        config["story"]["max_creation_restarts"] = 0
        write_json(self.path / "input/config.json", config)
        with self.assertRaises(ValidationError): self.pipeline.run_full(self.run, wait_seconds=0)
        self.assertEqual(sum(c["stage"] == "story_plan" for c, _ in self.provider.calls), 2)
        self.assertEqual(self.video.submissions, 0)
        count = len(self.provider.calls)
        with self.assertRaises(ValidationError): self.pipeline.run_full(self.run, wait_seconds=0)
        self.assertEqual(len(self.provider.calls), count)

    def test_legacy_sound_v1_retains_old_instructions_schema_and_compiler(self):
        self.assertEqual(sound_direction(1), SOUND_DIRECTION)
        self.assertNotIn(LOW_NOISE_DIRECTION, sound_direction(1))
        config = deepcopy(self.config)
        config["story"].update(sound_prompt_version=1, pacing_prompt_version=0)
        inputs = deepcopy(self.inputs)
        freeze_policy(inputs["production_constraints"], config)
        run = self.store.create(inputs, config)
        path = self.store.path(run)
        write_json(path / "input/skill.json", {})
        initialize(self.store, run, config)
        self.pipeline.plan(run)
        for stage, instruction in self.provider.instructions:
            self.assertNotIn("自动总时长优先", instruction)
            self.assertNotIn(LOW_NOISE_DIRECTION, instruction)
        saved = read_json(path / "shots/shot-001/video_request.json")
        self.assertEqual(saved["template_version"], "2.2")
        self.pipeline.plan(run)
        self.assertEqual(len(self.provider.calls), 6)
        self.pipeline.run_full(run, wait_seconds=0)
        self.assertEqual(read_json(path / "manifest.json")["template_version"], "2.2")

    def test_cli_run_and_plan_freeze_identical_preferred_policy_and_explicit_duration(self):
        raw = read_json(ROOT / "examples/events.json")
        raw["production_constraints"] = {}
        event = self.path / "events.json"; write_json(event, raw)
        for command, flags in (("run", []), ("plan", []), ("run", ["--duration", "40"])):
            args = cli.parser().parse_args(["--project", str(ROOT), command, "--events", str(event), *flags])
            run = cli.new_run(args, self.config, self.store)
            constraints = read_json(self.store.path(run) / "input/normalized.json")["production_constraints"]
            self.assertEqual(constraints["preferred_min_duration_seconds"], 15)
            self.assertEqual(constraints["preferred_max_duration_seconds"], 30)
            self.assertEqual(constraints["max_duration_seconds"], 60)
            self.assertEqual(constraints["duration_seconds"], 40 if flags else None)
            self.assertEqual(constraints["pacing_prompt_version"], 1)
        self.assertEqual(self.config["production"]["duration_seconds"], "")

    def test_imported_story_keeps_public_contract_without_timing_reason(self):
        self.pipeline.finish_story(self.run, self.template)
        self.assertEqual(self.store.load(self.run)["status"], "story_ready")
        self.assertNotIn("timing_reason", read_json(self.path / "story/result.json"))

    def test_legacy_full_story_generator_gets_new_budget_when_creativity_disabled(self):
        template = deepcopy(self.template)
        class FullProvider:
            calls = []
            def generate(inner, instruction, context):
                inner.calls.append(instruction)
                index = len(context["upstream"])
                if index < 3:
                    name = ("theme", "script", "visual_style")[index]
                    return {"status": "ready", name: template[name], "issues": []}, {}
                result = deepcopy(template)
                result["timing_reason"] = plan([12, 12])["timing_reason"]
                return result, {}
        provider = FullProvider()
        result = generate_story(self.path, self.inputs, {}, provider)
        self.assertNotIn("timing_reason", result)
        self.assertEqual(len(provider.calls), 4)
        self.assertIn("15–30秒", provider.calls[0])
        self.assertEqual(read_json(self.path / "story/timing.json")["actual_seconds"], 24)
        generate_story(self.path, self.inputs, {}, provider)
        self.assertEqual(len(provider.calls), 4)


if __name__ == "__main__":
    unittest.main()
