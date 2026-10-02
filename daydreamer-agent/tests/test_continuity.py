from copy import deepcopy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from daydreamer_agent.domain.continuity import STATE_FIELDS, validate_continuity, resolve_continuity_references, state_description
from daydreamer_agent.domain.errors import ValidationError


class ContinuityContractTests(unittest.TestCase):
    def setUp(self):
        self.state = dict(zip(STATE_FIELDS, ["门前一步，视高1.6米", "面向小巷深处", "缓慢前行", "店门右侧", "双手自然下垂", "右侧暖光不变"]))
        self.shots = [{"continuity": {"start": deepcopy(self.state), "end": deepcopy(self.state)}} for _ in range(2)]

    def test_identical_boundary_contract_accepted(self):
        validate_continuity(self.shots)

    def test_motion_reset_rejected(self):
        self.shots[1]["continuity"]["start"]["camera_motion"] = "突然停下"
        with self.assertRaises(ValidationError):
            validate_continuity(self.shots)

    def test_missing_spatial_state_rejected(self):
        del self.shots[0]["continuity"]["end"]["layout"]
        with self.assertRaises(ValidationError):
            validate_continuity(self.shots)

    def test_unstructured_description_is_insufficient(self):
        with self.assertRaises(ValidationError):
            validate_continuity([{"start_state": "接上镜", "end_state": "继续走"}])

    def test_explicit_previous_reference_expands_without_mutating_raw_output(self):
        for i, shot in enumerate(self.shots):
            shot["shot_id"] = str(i)
        self.shots[1]["continuity"]["start"] = {"from_shot": "0"}
        resolved = resolve_continuity_references({"shots": self.shots})
        validate_continuity(resolved["shots"])
        self.assertEqual(self.shots[1]["continuity"]["start"], {"from_shot": "0"})
        self.assertIsNot(resolved["shots"][0]["continuity"]["end"], resolved["shots"][1]["continuity"]["start"])

    def test_reference_cannot_skip_previous_shot_or_override_fields(self):
        for i, shot in enumerate(self.shots):
            shot["shot_id"] = str(i)
        for invalid in ({"from_shot": "2"}, {"from_shot": "0", "lighting": "changed"}):
            self.shots[1]["continuity"]["start"] = invalid
            with self.assertRaises(ValidationError):
                resolve_continuity_references({"shots": self.shots})

    def test_reference_expansion_never_silently_rewrites_full_states(self):
        self.shots[1]["continuity"]["start"]["lighting"] = "different"
        resolved = resolve_continuity_references({"shots": self.shots})
        with self.assertRaises(ValidationError):
            validate_continuity(resolved["shots"])

    def test_malformed_reference_output_reports_validation_error(self):
        for story in ({"shots": "wrong"}, {"shots": [None]}, {"shots": [{"shot_id": "0", "continuity": None}, {"continuity": {"start": {"from_shot": "0"}}}]}):
            with self.assertRaises(ValidationError):
                resolve_continuity_references(story)

    def test_duplicate_structured_summaries_are_converted_without_losing_fields(self):
        self.shots[0]["shot_id"] = "0"
        self.shots[0]["start_state"] = deepcopy(self.state)
        self.shots[0]["end_state"] = deepcopy(self.state)
        self.shots[1]["continuity"]["start"] = {"from_shot": "0"}
        self.shots[1]["start_state"] = {"from_shot": "0"}
        result = resolve_continuity_references({"shots": self.shots})
        for field in ("start_state", "end_state"):
            self.assertEqual(result["shots"][0][field], state_description(self.state))
        self.assertEqual(result["shots"][1]["start_state"], state_description(self.state))
        self.assertIsInstance(self.shots[0]["start_state"], dict)

    def test_conflicting_structured_summary_is_not_overwritten(self):
        self.shots[0]["start_state"] = {**self.state, "lighting": "conflict"}
        result = resolve_continuity_references({"shots": self.shots})
        self.assertEqual(result["shots"][0]["start_state"]["lighting"], "conflict")


if __name__ == "__main__":
    unittest.main()
