from copy import deepcopy
from pathlib import Path
import unittest
from unittest.mock import patch

import test_frame_chain
from test_creativity import CreativeProvider
from test_workflow import WorkflowMedia
from legacy_settings import load_settings
from daydreamer_agent.application.delivery import export_delivery
from daydreamer_agent.domain.errors import FieldValidationError, ModelOutputError, ProviderError, ValidationError
from daydreamer_agent.storage.files import read_json, write_json
from daydreamer_agent.story.creativity import initialize
from daydreamer_agent.story.seeded import recover_viewpoint_failure, use_seed_only
from daydreamer_agent.story.assembly import normalize_shot_prompt, VIEWPOINT_DIRECTIVE


class SeededTests(unittest.TestCase):
    def setUp(self):
        test_frame_chain.FrameChainTests.setUp(self)
        self.template = read_json(self.path / "story/result.json")
        self.inputs = read_json(self.path / "input/normalized.json")
        self.config = load_settings(test_frame_chain.ROOT)
        self.run = self.store.create(self.inputs, self.config)
        self.path = self.store.path(self.run)
        write_json(self.path / "input/skill.json", {})
        initialize(self.store, self.run, self.config)
        self.provider = CreativeProvider(self.template)
        self.pipeline.story_provider = self.provider
        self.pipeline.media = WorkflowMedia()

    def test_single_results_seed_and_full_workflow_resume(self):
        result = self.pipeline.run_full(self.run, wait_seconds=0)
        self.assertEqual(result["status"], "preview_ready")
        self.assertEqual([ctx["stage"] for ctx, _ in self.provider.calls], ["theme", "script", "visual_style", "story_plan", "shot", "shot"])
        self.assertFalse((self.path / "story/seeded/review.json").exists())
        seeds = []
        for context, sampling in self.provider.calls:
            self.assertIn(context["operation"], {"single_stage", "story_assembly"})
            self.assertNotIn("previous_work", context)
            self.assertNotIn("candidate_count", context)
            seeds.append(sampling["seed"])
        self.assertEqual(len(set(seeds)), 6)
        self.pipeline.run_full(self.run, wait_seconds=0)
        self.assertEqual(len(self.provider.calls), 6)
        result_story = read_json(self.path / "story/result.json")
        for key in ("theme", "script", "visual_style"):
            self.assertEqual(result_story[key], read_json(self.path / f"story/seeded/{key}.json")["output"][key])
        self.assertEqual(result_story["shots"][0]["continuity"]["end"], result_story["shots"][1]["continuity"]["start"])
        self.assertEqual(result_story["checks"], [])
        output = export_delivery(self.path, self.path / "delivery")
        self.assertTrue((Path(output["directory"]) / "创作随机种子.json").exists())
        self.assertFalse((Path(output["directory"]) / "创作差异检查.json").exists())

    def test_frozen_legacy_sound_checkpoints_keep_their_context_on_resume(self):
        self.pipeline.plan(self.run)
        shots = [ctx for ctx, _ in self.provider.calls if ctx["stage"] == "shot"]
        self.assertTrue(shots)
        self.assertTrue(all("previous_audio" not in ctx for ctx in shots))
        count = len(self.provider.calls)
        self.pipeline.plan(self.run)
        self.assertEqual(len(self.provider.calls), count)

    def test_new_task_does_not_load_history_and_has_fresh_seed(self):
        with patch("daydreamer_agent.story.creativity.previous_work", side_effect=AssertionError("no history")), \
             patch("daydreamer_agent.story.creativity.secrets.randbelow", return_value=12345):
            run = self.store.create(self.inputs, self.config)
            initialize(self.store, run, self.config)
        frozen = read_json(self.store.path(run) / "input/creativity.json")
        self.assertEqual(frozen["seed"], 12345)
        self.assertNotIn("previous_work", frozen)

    def test_viewpoint_default_preserves_raw_text_without_repair_or_restart(self):
        original = self.provider.generate
        def generate(instruction, context, **sampling):
            output, usage = original(instruction, context, **sampling)
            if context["stage"] == "shot":
                output["shot"]["prompt"]["画面内容"] = "我双手握紧车把，纸雕道路在前方延伸。"
                output["shot"]["prompt"]["景别"] = "第一人称主观视点"
            return output, usage
        with patch.object(self.provider, "generate", side_effect=generate):
            self.pipeline.plan(self.run)
        self.assertEqual(len(self.provider.calls), 6)
        story = read_json(self.path / "story/result.json")
        root = self.path / "story/seeded/assembly-v1"
        for shot in story["shots"]:
            raw = read_json(root / (shot["shot_id"] + ".json"))["output"]["shot"]["prompt"]
            self.assertNotIn("第一人称", raw["画面内容"])
            self.assertEqual(shot["prompt"]["画面内容"], VIEWPOINT_DIRECTIVE + raw["画面内容"])
            self.assertEqual(normalize_shot_prompt(shot["prompt"]), shot["prompt"])
        self.assertEqual(len(read_json(root / "assembly-record.json")["prompt_defaults"]), 2)
        self.assertEqual(len(read_json(self.path / "story/creation-attempts.json")["attempts"]), 1)

    def test_empty_prompt_is_not_masked_and_repair_names_the_field(self):
        original = self.provider.generate
        def generate(instruction, context, **sampling):
            output, usage = original(instruction, context, **sampling)
            if context.get("shot_number") == 1 and "repair" not in context:
                output["shot"]["prompt"]["画面内容"] = " "
            return output, usage
        with patch.object(self.provider, "generate", side_effect=generate):
            self.pipeline.plan(self.run)
        repairs = [c["repair"] for c, _ in self.provider.calls if "repair" in c]
        self.assertEqual(len(repairs), 1)
        self.assertEqual(repairs[0]["field"], "shot.prompt.画面内容")
        self.assertIn("非空字符串", repairs[0]["expected"])
        self.assertEqual(sum(c["stage"] == "theme" for c, _ in self.provider.calls), 1)

    def test_viewpoint_normalization_rejects_missing_or_invalid_prompt_fields(self):
        prompt = deepcopy(self.template["shots"][0]["prompt"])
        self.assertEqual(normalize_shot_prompt(prompt), prompt)
        for value in (None, [], 123, "", " \n"):
            with self.subTest(value=value):
                bad = {**prompt, "画面内容": value}
                with self.assertRaises(FieldValidationError) as error:
                    normalize_shot_prompt(bad)
                self.assertEqual(error.exception.field, "shot.prompt.画面内容")
        del prompt["画面内容"]
        with self.assertRaises(FieldValidationError):
            normalize_shot_prompt(prompt)

    def test_viewpoint_migration_is_narrow_and_once_only(self):
        reason = "创作阶段格式修正次数已用完：每镜画面内容需明确第一人称。"
        attempts = [{"seed": i, "status": "exhausted", "failed_stage": "shot-001", "reason": reason} for i in range(3)]
        state = {"max_restarts": 2, "attempts": deepcopy(attempts)}
        self.assertTrue(recover_viewpoint_failure(state))
        self.assertEqual(state["attempts"][:2], attempts[:2])
        self.assertEqual(state["attempts"][-1]["seed"], 2)
        self.assertEqual(state["attempts"][-1]["status"], "active")
        self.assertEqual(state["attempts"][-1]["viewpoint_failure_recovery"]["reason"], reason)
        state["attempts"][-1].update(status="exhausted", failed_stage="shot-001", reason=reason)
        self.assertFalse(recover_viewpoint_failure(state))
        self.assertEqual(state["attempts"][-1]["status"], "exhausted")
        for stage, detail in (("theme", reason), ("shot-001", "other failure")):
            other = {"attempts": [{"seed": 1, "status": "exhausted", "failed_stage": stage, "reason": detail}]}
            self.assertFalse(recover_viewpoint_failure(other))
            self.assertEqual(other["attempts"][0]["status"], "exhausted")

    def test_legacy_viewpoint_failure_reuses_saved_response_at_exhausted_budget(self):
        from daydreamer_agent.story import assembly
        original = self.provider.generate
        def generate(instruction, context, **sampling):
            output, usage = original(instruction, context, **sampling)
            if context["stage"] == "shot":
                output["shot"]["prompt"]["画面内容"] = "我双手握住纸雕车把。"
            return output, usage
        # Reproduce the old gate to create exhausted response files with real request fingerprints.
        with patch.object(self.provider, "generate", side_effect=generate), \
             patch.object(assembly, "normalize_shot_prompt", side_effect=deepcopy):
            with self.assertRaises(ValidationError):
                self.pipeline.plan(self.run)
        path = self.path / "story/creation-attempts.json"
        state = read_json(path)
        del state["viewpoint_defaults_version"]
        write_json(path, state)
        self.provider.calls.clear()
        self.pipeline.plan(self.run)
        # Last attempt's first shot is accepted from disk; only missing second shot is requested.
        self.assertEqual([c.get("shot_number") for c, _ in self.provider.calls], [2])
        saved = read_json(path)
        self.assertEqual(len(saved["attempts"]), 3)
        self.assertEqual(saved["attempts"][-1]["seed"], state["attempts"][-1]["seed"])
        self.assertEqual(saved["attempts"][-1]["status"], "ready")

    def test_failed_second_shot_resumes_without_resending_first_shot(self):
        original = self.provider.generate
        interrupted = False
        def generate(instruction, context, **sampling):
            nonlocal interrupted
            if context.get("shot_number") == 2 and not interrupted:
                interrupted = True
                raise ProviderError("connection lost")
            return original(instruction, context, **sampling)
        with patch.object(self.provider, "generate", side_effect=generate):
            with self.assertRaises(ProviderError):
                self.pipeline.plan(self.run)
            self.assertTrue((self.path / "story/seeded/assembly-v1/shot-001.json").exists())
            self.pipeline.plan(self.run)
        self.assertEqual(sum(c.get("shot_number") == 1 for c, _ in self.provider.calls), 1)
        self.assertEqual(sum(c.get("stage") == "story_plan" for c, _ in self.provider.calls), 1)
        second = next(c for c, _ in self.provider.calls if c.get("shot_number") == 2)
        self.assertNotIn("input", second)
        self.assertNotIn("script", second["locked"])
        self.assertNotIn("shots", second)
        self.assertEqual(len(second["current_beats"]), 1)

    def test_bad_single_shot_is_repaired_locally_and_cannot_rewrite_locked_theme(self):
        original = self.provider.generate
        repaired = False
        def generate(instruction, context, **sampling):
            nonlocal repaired
            output, usage = original(instruction, context, **sampling)
            if context.get("shot_number") == 2 and not repaired:
                repaired = True
                output["theme"] = {"title": "禁止模型改写这个字段"}
            return output, usage
        with patch.object(self.provider, "generate", side_effect=generate):
            self.pipeline.plan(self.run)
        stages = [c["stage"] for c, _ in self.provider.calls]
        self.assertEqual(stages, ["theme", "script", "visual_style", "story_plan", "shot", "shot", "shot"])
        repair = self.provider.calls[-1][0]["repair"]
        self.assertNotIn("script", repair["invalid_output"])
        self.assertNotIn("shots", repair["invalid_output"])
        story = read_json(self.path / "story/result.json")
        self.assertEqual(story["theme"], self.template["theme"])

    def test_legacy_exhausted_story_reuses_frozen_stages_once_without_resetting_budget(self):
        self.provider.fail_stage = "story_plan"
        with self.assertRaises(ProviderError):
            self.pipeline.plan(self.run)
        path = self.path / "story/creation-attempts.json"
        state = read_json(path)
        del state["assembly_version"]
        state["attempts"][0].update(status="exhausted", failed_stage="story", reason="legacy full package truncated")
        write_json(path, state)
        self.provider.calls.clear()
        self.pipeline.plan(self.run)
        self.assertEqual([c["stage"] for c, _ in self.provider.calls], ["story_plan", "shot", "shot"])
        saved = read_json(path)
        self.assertEqual(len(saved["attempts"]), 1)
        self.assertEqual(saved["attempts"][0]["legacy_failure"]["stage"], "story")

    def test_duration_plan_is_balanced_locally_before_any_shot(self):
        original = self.provider.generate
        bad = False
        def generate(instruction, context, **sampling):
            nonlocal bad
            output, usage = original(instruction, context, **sampling)
            if context["stage"] == "story_plan" and not bad:
                bad = True
                output["shots"][0]["duration_seconds"] += 1
            return output, usage
        with patch.object(self.provider, "generate", side_effect=generate):
            self.pipeline.plan(self.run)
        self.assertEqual([c["stage"] for c, _ in self.provider.calls], ["theme", "script", "visual_style", "story_plan", "shot", "shot"])
        root = self.path / "story/seeded/assembly-v1"
        timing = read_json(root / "plan-timing.json")
        self.assertEqual(timing["proposed_seconds"], [4, 3])
        self.assertEqual(timing["allocated_seconds"], [3, 3])
        self.assertEqual(read_json(root / "plan.json")["output"]["shots"][0]["duration_seconds"], 4)
        self.pipeline.plan(self.run)
        self.assertEqual(len(self.provider.calls), 6)

    def test_exhausted_plan_timing_recovers_cached_plan_without_new_creative_calls(self):
        from daydreamer_agent.story import assembly
        with patch.object(assembly, "validate_plan", side_effect=ValidationError("分镜规划总时长与制作约束不符。")):
            with self.assertRaises(ValidationError):
                self.pipeline.plan(self.run)
        state_path = self.path / "story/creation-attempts.json"
        state = read_json(state_path)
        del state["plan_timing_version"]
        write_json(state_path, state)
        self.provider.calls.clear()
        self.pipeline.plan(self.run)
        self.assertEqual([c["stage"] for c, _ in self.provider.calls], ["shot", "shot"])
        saved = read_json(state_path)
        self.assertEqual(len(saved["attempts"]), 3)
        self.assertEqual(saved["attempts"][-1]["seed"], state["attempts"][-1]["seed"])
        self.assertEqual(saved["attempts"][-1]["status"], "ready")

    def test_absent_memory_cannot_acquire_model_invented_memory_references(self):
        original = self.provider.generate
        def generate(instruction, context, **sampling):
            output, usage = original(instruction, context, **sampling)
            if context["stage"] == "story_plan":
                output["used_memory_ids"] = ["event-is-not-memory"]
            return output, usage
        # The fixture may supply a memory world; this test explicitly has none.
        path = self.path / "input/normalized.json"
        inputs = read_json(path)
        inputs["story_memory"] = None
        # Exercise assembly directly, leaving the run input digest untouched.
        from daydreamer_agent.story.assembly import generate_assembly
        prior = {k: {k: self.template[k]} for k in ("theme", "script", "visual_style")}
        with patch.object(self.provider, "generate", side_effect=generate):
            story = generate_assembly(self.path / "memory-test", inputs, prior, self.provider, 123, 1, lambda _: None)
        self.assertEqual(story["memory_basis"]["used_ids"], [])
        self.assertEqual(len(self.provider.calls), 3)
        record = read_json(self.path / "memory-test/assembly-v1/plan-timing.json")
        self.assertEqual(record["proposed_memory_ids"], ["event-is-not-memory"])
        self.assertEqual(record["used_memory_ids"], [])

    def test_resume_reuses_theme_and_same_unfinished_seed(self):
        self.provider.fail_stage = "script"
        with self.assertRaises(ProviderError):
            self.pipeline.plan(self.run)
        failed_seed = self.provider.calls[-1][1]["seed"]
        self.pipeline.plan(self.run)
        self.assertEqual(self.provider.calls[2][1]["seed"], failed_seed)
        self.assertEqual(sum(ctx["stage"] == "theme" for ctx, _ in self.provider.calls), 1)

    def test_migration_keeps_selected_stages_without_selecting_pending_style(self):
        frozen = read_json(self.path / "input/creativity.json")
        old = {"version": 1, "seed": frozen["seed"], "policy": {"temperature": 1.0}, "previous_work": {"title": "old"}}
        write_json(self.path / "input/creativity.json", old)
        round_path = self.path / "story/diversity/round-01"
        choices = {}
        for name in ("theme", "script", "visual_style"):
            write_json(round_path / f"{name}.json", {"output": {"candidates": [{"candidate_id": "chosen", name: self.template[name]}]}})
            if name != "visual_style":
                choices[name] = {"candidate_id": "chosen"}
        write_json(round_path / "choices.json", choices)
        self.pipeline.plan(self.run)
        self.assertEqual([ctx["stage"] for ctx, _ in self.provider.calls], ["visual_style", "story_plan", "shot", "shot"])
        self.assertEqual(read_json(self.path / "input/creativity-before-seed-only.json"), old)
        self.assertEqual(set(read_json(self.path / "input/reused-story-stages.json")), {"theme", "script"})
        self.assertEqual(use_seed_only(self.path)["seed"], frozen["seed"])

    def test_malformed_response_saved_repaired_once_and_budget_survives_resume(self):
        error = ModelOutputError("invalid JSON", kind="invalid_json", response={"choices": [], "usage": {"completion_tokens": 12}})
        with patch.object(self.provider, "generate", side_effect=error) as generate:
            for _ in range(2):
                with self.assertRaises(ValidationError):
                    self.pipeline.plan(self.run)
            self.assertEqual(generate.call_count, 6)  # Initial creation + two restarts, two format attempts each.
            self.assertNotEqual(generate.call_args_list[0].kwargs["seed"], generate.call_args_list[1].kwargs["seed"])
        response = read_json(self.path / "story/seeded/responses/theme-1.json")
        self.assertEqual(response["raw_response"]["usage"]["completion_tokens"], 12)
        self.assertEqual(self.video.submissions, 0)

    def test_story_failure_restarts_at_theme_with_new_seed_and_preserves_outputs(self):
        original = self.provider.generate
        failures = 0
        def generate(instruction, context, **sampling):
            nonlocal failures
            if context["stage"] == "shot" and failures < 2:
                failures += 1
                output, usage = original(instruction, context, **sampling)
                output["shot"]["end"] = {"from_shot": "shot-01"}
                return output, usage
            return original(instruction, context, **sampling)
        with patch.object(self.provider, "generate", side_effect=generate):
            self.assertEqual(self.pipeline.plan(self.run)["status"], "story_ready")
        stages = [ctx["stage"] for ctx, _ in self.provider.calls]
        self.assertEqual(stages, ["theme", "script", "visual_style", "story_plan", "shot", "shot", "theme", "script", "visual_style", "story_plan", "shot", "shot"])
        state = read_json(self.path / "story/creation-attempts.json")
        self.assertEqual([a["status"] for a in state["attempts"]], ["exhausted", "ready"])
        self.assertNotEqual(state["attempts"][0]["seed"], state["attempts"][1]["seed"])
        self.assertTrue((self.path / "story/seeded/assembly-v1/responses/shot-001-2.json").exists())
        self.assertTrue((self.path / "story/seeded-restart-01/assembly-v1/result.json").exists())
        self.assertFalse((self.path / "story/seeded-restart-01/review.json").exists())

    def test_network_failure_during_restart_resumes_same_seed_and_budget(self):
        original = self.provider.generate
        count = 0
        def generate(instruction, context, **sampling):
            nonlocal count
            count += 1
            if count <= 2:
                return {"status": "ready", "issues": []}, {}
            raise ProviderError("network")
        with patch.object(self.provider, "generate", side_effect=generate):
            with self.assertRaises(ProviderError):
                self.pipeline.plan(self.run)
        state = read_json(self.path / "story/creation-attempts.json")
        self.assertEqual(len(state["attempts"]), 2)
        seed = state["attempts"][-1]["seed"]
        self.pipeline.plan(self.run)
        state = read_json(self.path / "story/creation-attempts.json")
        self.assertEqual(len(state["attempts"]), 2)
        self.assertEqual(state["attempts"][-1]["seed"], seed)

    def test_network_failure_and_material_conflict_do_not_restart(self):
        self.provider.fail_stage = "theme"
        with self.assertRaises(ProviderError):
            self.pipeline.plan(self.run)
        self.assertEqual(len(read_json(self.path / "story/creation-attempts.json")["attempts"]), 1)
        with patch.object(self.provider, "generate", return_value=({"status": "needs_resolution", "issues": ["补充素材"]}, {})):
            self.assertEqual(self.pipeline.plan(self.run)["status"], "needs_resolution")
        self.assertEqual(len(read_json(self.path / "story/creation-attempts.json")["attempts"]), 1)


if __name__ == "__main__":
    unittest.main()
