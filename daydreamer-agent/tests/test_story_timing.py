from copy import deepcopy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from daydreamer_agent.domain.errors import FieldValidationError
from daydreamer_agent.story.timing import fit_plan_timing, recover_timing_failure, validate_timing_constraints


def plan(durations):
    return {"shots": [{"duration_seconds": d, "beat_ids": [f"beat-{i}"], "action_range": f"action-{i}"} for i, d in enumerate(durations)]}


class TimingTests(unittest.TestCase):
    def test_actual_failure_allocates_four_three_second_shots_without_rewriting_actions(self):
        for durations in ([3, 4, 3, 2], [3, 4, 3, 5]):
            raw = plan(durations)
            before = deepcopy(raw)
            result = fit_plan_timing(raw, {"duration_seconds": 12})
            self.assertEqual([s["duration_seconds"] for s in result["shots"]], [3, 3, 3, 3])
            self.assertEqual(raw, before)
            for a, b in zip(raw["shots"], result["shots"]):
                self.assertEqual(a["beat_ids"], b["beat_ids"])
                self.assertEqual(a["action_range"], b["action_range"])
            self.assertEqual(fit_plan_timing(result, {"duration_seconds": 12}), result)

    def test_bounded_integer_totals_for_all_feasible_small_plans(self):
        for count in range(1, 9):
            for total in range(count * 3, count * 15 + 1):
                raw = plan([float((i + 1) ** 2) / 2 for i in range(count)])
                result = fit_plan_timing(raw, {"duration_seconds": total})
                values = [s["duration_seconds"] for s in result["shots"]]
                self.assertEqual(sum(values), total)
                self.assertTrue(all(type(v) is int and 3 <= v <= 15 for v in values))

    def test_valid_timing_preserved_and_missing_suggestions_supported(self):
        raw = plan([3, 6, 3])
        self.assertEqual(fit_plan_timing(raw, {"duration_seconds": 12.0}), raw)
        self.assertEqual([s["duration_seconds"] for s in fit_plan_timing(plan([None, None]), {"duration_seconds": 7})["shots"]], [4, 3])
        self.assertEqual(fit_plan_timing(plan([None]), {}), plan([None]))

    def test_impossible_count_requires_replanning_instead_of_dropping_actions(self):
        with self.assertRaises(FieldValidationError) as exc:
            fit_plan_timing(plan([2, 2, 2, 2, 4]), {"duration_seconds": 12})
        self.assertEqual(exc.exception.field, "shots")
        self.assertIn("1至4", exc.exception.expected)

    def test_automatic_duration_preserves_shorter_plan_and_caps_long_plan(self):
        constraints = {"duration_seconds": None, "max_duration_seconds": 60}
        short = plan([8, 10, 12])
        self.assertEqual(fit_plan_timing(short, constraints), short)
        long = fit_plan_timing(plan([15] * 5), constraints)
        self.assertEqual(sum(s["duration_seconds"] for s in long["shots"]), 60)
        self.assertEqual(len(long["shots"]), 5)
        with self.assertRaises(FieldValidationError):
            fit_plan_timing(plan([3] * 21), constraints)
        for target in (61, 90):
            with self.assertRaises(FieldValidationError):
                validate_timing_constraints({**constraints, "duration_seconds": target})

    def test_automatic_duration_fills_missing_suggestions_and_respects_single_take(self):
        constraints = {"max_duration_seconds": 60, "continuity_mode": "single_take"}
        result = fit_plan_timing(plan([None]), constraints)
        self.assertEqual(result["shots"][0]["duration_seconds"], 6)
        self.assertEqual(fit_plan_timing(plan([30]), constraints)["shots"][0]["duration_seconds"], 15)

    def test_invalid_values_and_impossible_constraints_are_rejected(self):
        for value in (False, "3", -1, 0, float("nan"), float("inf")):
            with self.subTest(value=value), self.assertRaises(FieldValidationError):
                fit_plan_timing(plan([value]), {"duration_seconds": 6})
        for constraints in ({"duration_seconds": 2}, {"duration_seconds": 12.5},
                            {"duration_seconds": 16, "continuity_mode": "single_take"},
                            {"duration_seconds": 31, "shot_limit": 2}):
            with self.subTest(constraints=constraints), self.assertRaises(FieldValidationError):
                validate_timing_constraints(constraints)

    def test_migration_is_narrow_once_only_and_preserves_budget(self):
        reason = "创作阶段格式修正次数已用完：分镜规划总时长与制作约束不符。"
        state = {"max_restarts": 2, "attempts": [{"seed": 8, "status": "exhausted", "failed_stage": "plan", "reason": reason}]}
        self.assertTrue(recover_timing_failure(state))
        self.assertEqual(state["attempts"][0]["seed"], 8)
        self.assertEqual(state["max_restarts"], 2)
        state["attempts"][0].update(status="exhausted", failed_stage="plan", reason=reason)
        self.assertFalse(recover_timing_failure(state))
        del state["plan_timing_version"]
        state["attempts"][0]["reason"] = "another failure"
        self.assertFalse(recover_timing_failure(state))
