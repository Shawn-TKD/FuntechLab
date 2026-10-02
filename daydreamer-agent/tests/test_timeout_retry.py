from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from daydreamer_agent.application.settings import video_retry_policy
from daydreamer_agent.domain.errors import ProviderError, ValidationError
from daydreamer_agent.storage.files import read_json, write_json
import test_frame_chain

RUNNING = {"task_status": "RUNNING"}
SUCCESS = {"task_status": "SUCCEEDED", "video_url": "https://example.com/video.mp4"}
CLOCK = "daydreamer_agent.application.pipeline.time"


class TimeoutRetryTests(unittest.TestCase):
    def setUp(self):
        test_frame_chain.FrameChainTests.setUp(self)

    def stalled_second(self, age=100):
        self.video.results = [SUCCESS, RUNNING]
        self.pipeline.render(self.run)
        state = self.store.shot(self.run, "shot-02")
        state["submitted_at"] = age
        self.store.save_shot(self.run, state)

    def test_timeout_reuses_same_request_and_frame_and_preserves_old_task(self):
        self.stalled_second()
        old_payload = self.video.payloads[1]
        self.video.results = [RUNNING, RUNNING, SUCCESS]
        with patch(CLOCK + ".time", return_value=401):
            self.assertEqual(self.pipeline.render(self.run)["status"], "awaiting_audio")
        self.assertEqual(self.video.submissions, 3)
        self.assertEqual(self.video.payloads[2], old_payload)
        state = self.store.shot(self.run, "shot-02")
        self.assertEqual((state["attempt"], state["timeout_resubmissions"]), (2, 1))
        self.assertEqual(state["supersedes_task_id"], "task-2")
        self.assertEqual(self.store.shot(self.run, "shot-01")["attempt"], 1)
        old = read_json(self.path / "shots/shot-02/attempts/1/task.json")
        self.assertEqual(old["task_id"], "task-2")
        self.assertEqual(old["superseded_reason"], "generation_timeout")
        self.assertFalse(old["remote_task_canceled"])

    def test_final_query_success_avoids_resubmission(self):
        self.stalled_second()
        self.video.results = [RUNNING, SUCCESS]
        with patch(CLOCK + ".time", return_value=401):
            self.pipeline.render(self.run)
        self.assertEqual(self.video.submissions, 2)
        self.assertEqual(self.store.shot(self.run, "shot-02")["attempt"], 1)

    def test_before_threshold_does_not_resubmit(self):
        self.stalled_second()
        self.video.results = [RUNNING]
        with patch(CLOCK + ".time", return_value=399):
            self.pipeline.render(self.run)
        self.assertEqual(self.video.submissions, 2)

    def test_retry_cap_survives_restarts_and_late_success_can_be_downloaded(self):
        self.stalled_second()
        self.video.results = [RUNNING, RUNNING, RUNNING]
        with patch(CLOCK + ".time", return_value=401):
            self.pipeline.render(self.run)
        self.assertEqual(self.video.submissions, 3)
        for _ in range(2):
            self.video.results = [RUNNING, RUNNING]
            with patch(CLOCK + ".time", return_value=801):
                result = self.pipeline.render(self.run)
            self.assertEqual(result["status"], "needs_resolution")
            self.assertEqual(result["reason"], "timeout_resubmissions_exhausted")
            self.assertEqual(self.video.submissions, 3)
        self.video.results = [SUCCESS]
        self.assertEqual(self.pipeline.render(self.run)["status"], "awaiting_audio")
        self.assertEqual(self.video.submissions, 3)

    def test_query_error_never_triggers_resubmission(self):
        self.stalled_second()
        with patch.object(self.video, "query", side_effect=[RUNNING, ProviderError("query failed", retryable=True)]), patch(CLOCK + ".time", return_value=401):
            with self.assertRaises(ProviderError):
                self.pipeline.render(self.run)
        self.assertEqual(self.video.submissions, 2)

    def test_uncertain_retry_submission_stops_without_further_submissions(self):
        self.stalled_second()
        self.video.results = [RUNNING, RUNNING]
        self.video.error = ProviderError("submit timed out", uncertain=True)
        with patch(CLOCK + ".time", return_value=401), self.assertRaises(ProviderError):
            self.pipeline.render(self.run)
        self.assertEqual(self.store.shot(self.run, "shot-02")["status"], "submission_unknown")
        self.pipeline.render(self.run)
        self.assertEqual(self.video.submissions, 3)

    def test_missing_legacy_timestamp_starts_observation_window(self):
        self.stalled_second()
        state = self.store.shot(self.run, "shot-02")
        del state["submitted_at"]
        self.store.save_shot(self.run, state)
        self.video.results = [RUNNING]
        with patch(CLOCK + ".time", return_value=401):
            self.pipeline.render(self.run)
        self.assertEqual(self.video.submissions, 2)
        self.assertEqual(self.store.shot(self.run, "shot-02")["submitted_at"], 401)

    def test_zero_budget_disables_automatic_resubmission(self):
        self.stalled_second()
        config = read_json(self.path / "input/config.json")
        config["video"]["max_timeout_resubmissions"] = 0
        write_json(self.path / "input/config.json", config)
        self.video.results = [RUNNING]
        with patch(CLOCK + ".time", return_value=401):
            self.pipeline.render(self.run)
        self.assertEqual(self.video.submissions, 2)

    def test_existing_descendant_is_not_silently_mixed_or_discarded(self):
        # Simulate an interrupted historical chain with later work already present.
        self.video.results = [RUNNING]
        self.pipeline.render(self.run)
        first = self.store.shot(self.run, "shot-01")
        first["submitted_at"] = 100
        self.store.save_shot(self.run, first)
        self.store.save_shot(self.run, {"shot_id": "shot-02", "attempt": 1, "status": "running", "task_id": "existing-child"})
        self.video.results = [RUNNING, RUNNING]
        with patch(CLOCK + ".time", return_value=401):
            result = self.pipeline.render(self.run)
        self.assertEqual(result["reason"], "timeout_has_existing_descendants")
        self.assertEqual(self.video.submissions, 1)

    def test_live_wait_loop_automatically_retries_and_stops_at_budget(self):
        config = read_json(self.path / "input/config.json")
        config["video"].update(generation_timeout_seconds=5, max_timeout_resubmissions=1)
        write_json(self.path / "input/config.json", config)
        clock = [0.0]
        def sleep(seconds):
            clock[0] += seconds
        with patch(CLOCK + ".time", side_effect=lambda: clock[0]), patch(CLOCK + ".monotonic", side_effect=lambda: clock[0]), patch(CLOCK + ".sleep", side_effect=sleep), patch.object(self.video, "query", return_value=RUNNING):
            result = self.pipeline.render(self.run, wait_seconds=30)
        self.assertEqual(result["status"], "needs_resolution")
        self.assertEqual(self.video.submissions, 2)
        self.assertEqual(clock[0], 10)

    def test_policy_validation(self):
        for fields in ({"generation_timeout_seconds": 0}, {"generation_timeout_seconds": True}, {"max_timeout_resubmissions": -1}, {"max_timeout_resubmissions": 6}, {"max_timeout_resubmissions": 1.5}):
            with self.assertRaises(ValidationError):
                video_retry_policy({"video": fields})

    def test_downstream_uses_new_attempt_of_timed_out_predecessor(self):
        from copy import deepcopy
        story = read_json(self.path / "story/result.json")
        third = deepcopy(story["shots"][-1])
        third["shot_id"] = "shot-03"
        story["shots"][-1]["transition_to_next"] = "继续前行"
        story["shots"].append(third)
        inputs = read_json(self.path / "input/normalized.json")
        inputs["production_constraints"]["duration_seconds"] = 9
        write_json(self.path / "input/normalized.json", inputs)
        self.pipeline.finish_story(self.run, story)
        self.stalled_second()
        self.video.results = [RUNNING, RUNNING, SUCCESS, SUCCESS]
        with patch(CLOCK + ".time", return_value=401):
            self.pipeline.render(self.run)
        self.assertEqual(self.video.submissions, 4)
        third = self.store.shot(self.run, "shot-03")
        self.assertEqual(third["predecessor"], self.pipeline.dependency(self.store.shot(self.run, "shot-02")))
        self.assertEqual(third["predecessor"]["attempt"], 2)


if __name__ == "__main__":
    unittest.main()
