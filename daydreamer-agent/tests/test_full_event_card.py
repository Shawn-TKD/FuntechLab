from copy import deepcopy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from daydreamer_agent.domain.events import normalize
from daydreamer_agent.domain.errors import ValidationError
from daydreamer_agent.story.validation import validate_sources


class FullCardTests(unittest.TestCase):
    def setUp(self):
        self.card = {"basic_info": {"event_id": "alley-1"}, "event_description": {
            "summary": "在门前停步", "sequence": [{"order": 1, "description": "停步", "source_refs": [{"material_id": "m1", "start_seconds": 0, "end_seconds": 4}]}]},
            "key_frames": [{"frame_id": "f1", "image_uri": None, "timestamp_seconds": 2, "description": "门内暖光"}],
            "audio_info": [{"description": "风铃声", "source_refs": []}], "details_to_preserve": [{"description": "暖光"}]}

    def test_full_card_preserves_evidence_without_mutating_source(self):
        original = deepcopy(self.card)
        result = normalize(self.card, duration=12, ratio="16:9", resolution="720P")
        card = result["event_cards"][0]
        self.assertEqual(self.card, original)
        self.assertEqual(card["event_id"], "alley-1")
        self.assertEqual(card["summary"], "在门前停步")
        self.assertEqual(card["source_refs"][0]["material_id"], "m1")
        self.assertNotIn("image_uri", card["source_refs"][1])
        self.assertIn("未读取", card["unknowns"][-1])
        self.assertEqual(card["sound_clues"][0]["description"], "风铃声")

    def test_wrapped_array_supported(self):
        self.assertEqual(normalize({"event_cards": [self.card]})["event_cards"][0]["event_id"], "alley-1")

    def test_normalization_is_idempotent(self):
        once = normalize(self.card)
        self.assertEqual(normalize(once), once)

    def test_bad_sequence_rejected(self):
        self.card["event_description"]["sequence"] = "wrong"
        with self.assertRaises(ValidationError):
            normalize(self.card)

    def test_simple_single_card_supported(self):
        self.assertEqual(normalize({"summary": "散步"})["event_cards"][0]["summary"], "散步")

    def test_refs_may_use_existing_audio_or_frame_ids(self):
        self.card["audio_info"][0]["audio_id"] = "a1"
        card = normalize(self.card)["event_cards"][0]
        validate_sources([{"audio_id": "a1"}, {"frame_id": "f1"}], ["alley-1"], {"alley-1": card})
        with self.assertRaises(ValidationError):
            validate_sources([{"audio_id": "invented"}], ["alley-1"], {"alley-1": card})

    def test_frame_reference_need_not_repeat_local_image_path(self):
        self.card["key_frames"][0].update(material_id="m1", image_uri="C:/frames/f1.jpg")
        card = normalize(self.card)["event_cards"][0]
        ref = {"frame_id": "f1", "material_id": "m1", "timestamp_seconds": 2}
        validate_sources([ref], ["alley-1"], {"alley-1": card})
        for wrong in ({**ref, "timestamp_seconds": 3}, {**ref, "material_id": "unknown"}, {**ref, "image_uri": "C:/wrong.jpg"}):
            with self.assertRaises(ValidationError):
                validate_sources([wrong], ["alley-1"], {"alley-1": card})


if __name__ == "__main__":
    unittest.main()
