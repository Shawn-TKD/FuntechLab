from legacy_settings import load_settings
from copy import deepcopy
import io
import json
from pathlib import Path
import sys
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from daydreamer_agent import cli
from daydreamer_agent.application.settings import Credentials
from daydreamer_agent.domain.errors import FieldValidationError, ProviderError, ValidationError
from daydreamer_agent.domain.events import normalize
from daydreamer_agent.event_extraction.media import chunk_ranges, inspect_video, prepare_chunk
from daydreamer_agent.event_extraction.pipeline import EventExtraction
from daydreamer_agent.event_extraction.validation import validate_card, validate_response
from daydreamer_agent.media.ffmpeg import Media
from daydreamer_agent.providers.qwen_vision import MAX_VIDEO_BYTES, QwenVision
from daydreamer_agent.storage.files import read_json, write_json
from daydreamer_agent.storage.jobs_sqlite import JobStore


def card_for(context):
    card = read_json(ROOT / "resources/event-card/template.json")
    material = context["material_id"]
    card["basic_info"].update(event_id=context["event_id"], title="杯子向右移动", scene="桌面上的杯子向右移动。")
    card["event_description"] = {"summary": "杯子沿桌面向右移动后停下。", "sequence": [
        {"order": 1, "description": "杯子向右移动。", "source_refs": [{"material_id": material, "start_seconds": 0, "end_seconds": context["duration_seconds"]}]}]}
    card["key_frames"] = [{"frame_id": context["allowed_frame_ids"][0], "image_uri": None, "material_id": material,
                           "timestamp_seconds": 1, "description": "杯子位于桌面中央。", "reference_focus": ["杯子和桌面的空间关系"]}]
    card["details_to_preserve"] = [{"description": "杯子沿桌面向右移动。", "value": "保留现实动作骨架。", "frame_ids": [context["allowed_frame_ids"][0]], "audio_ids": [], "sequence_orders": [1]}]
    card["selection_info"]["reason"] = "动作变化可辨认。"
    return card


class FakeVision:
    model = "qwen3.8-max"
    def __init__(self):
        self.calls = []
        self.responses = []
    def generate(self, instruction, context, schema, **kwargs):
        self.calls.append((deepcopy(context), kwargs))
        if self.responses:
            response = self.responses.pop(0)
            if isinstance(response, Exception):
                raise response
            if callable(response):
                return response(context)
            return response
        if "segment_cards" in context:
            card = deepcopy(context["segment_cards"][0])
            for extra in context["segment_cards"][1:]:
                for step in extra["event_description"]["sequence"]:
                    step = deepcopy(step)
                    step["order"] = len(card["event_description"]["sequence"]) + 1
                    card["event_description"]["sequence"].append(step)
                card["key_frames"].extend(deepcopy(extra["key_frames"]))
        else:
            card = card_for(context)
        return {"content": json.dumps({"status": "ready", "issues": [], "card": card}), "finish_reason": "stop", "usage": {"total_tokens": 123}}


class TwoStageVision(FakeVision):
    def __init__(self):
        super().__init__()
        self.observed = []
        self.observation = {"status": "ready", "issues": [], "observations": "0至6秒杯子沿桌面向右移动。1秒杯子位于桌面中央。"}

    def observe(self, context, **kwargs):
        self.observed.append((deepcopy(context), kwargs))
        return {"content": json.dumps(self.observation), "finish_reason": "stop", "usage": {}}


class ExtractionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        for relative in ("schemas/life_event_card.schema.json", "prompts/event_extraction.md"):
            target = self.root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((ROOT / relative).read_bytes())
        self.source = self.root / "素材 空格.mp4"
        self.source.write_bytes(b"source-video")
        self.store = JobStore(self.root / "data/extractions", self.root / "jobs.sqlite3")
        self.media = Mock()
        self.media.probe.return_value = {"streams": [{"index": 0, "codec_type": "video", "duration": "6", "width": 640, "height": 360}, {"codec_type": "audio"}]}
        def prepare(media, source, target, material, start, end):
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(f"silent-{start}-{end}".encode())
        patcher = patch("daydreamer_agent.event_extraction.pipeline.prepare_chunk", side_effect=prepare)
        self.prepare = patcher.start()
        self.addCleanup(patcher.stop)
        self.provider = FakeVision()
        self.extractor = EventExtraction(self.root, self.store, self.media, self.provider, progress=lambda _: None)
        self.config = load_settings(ROOT)
        self.run = self.extractor.create(self.source, self.config)

    def test_complete_exports_text_only_card_and_existing_agent_reads_it(self):
        result = self.extractor.run(self.run)
        card = read_json(result["event_card"])
        self.assertEqual(result["status"], "completed")
        self.assertFalse(card["audio_info"])
        self.assertIsNone(card["key_frames"][0]["image_uri"])
        self.assertEqual(normalize(card)["event_cards"][0]["event_id"], card["basic_info"]["event_id"])
        self.assertFalse(list(self.root.rglob("*.jpg")) + list(self.root.rglob("*.png")))
        record = read_json(Path(result["directory"]) / "提取记录.json")
        self.assertEqual(record["audio_review_status"], "not_analyzed")
        self.assertTrue(read_json(Path(result["directory"]) / "素材引用表.json")["materials"][0]["has_audio"])

    def test_two_stage_video_then_text_schema_and_reuse_after_interruption(self):
        provider = TwoStageVision()
        self.extractor.provider = provider
        provider.responses = [ProviderError("text request interrupted")]
        with self.assertRaises(ProviderError): self.extractor.run(self.run)
        self.assertEqual(len(provider.observed), 1)
        self.assertEqual(self.extractor.run(self.run)["status"], "completed")
        self.assertEqual(len(provider.observed), 1)
        self.assertIsNone(provider.calls[-1][1]["video"])
        self.assertIn("visual_observations", provider.calls[-1][0])
        self.extractor.run(self.run)
        self.assertEqual(len(provider.calls), 2)

    def test_two_stage_preserves_old_failed_responses(self):
        root, originals = self.seed_old_empty_responses()
        provider = TwoStageVision()
        self.extractor.provider = provider
        self.assertEqual(self.extractor.run(self.run)["status"], "completed")
        for path, data in originals.items(): self.assertEqual(path.read_bytes(), data)
        self.assertEqual(len(provider.observed), 1)
        self.assertTrue((root / "structured-v4/result.json").exists())

    def test_two_stage_does_not_bypass_card_checks_or_reset_budget(self):
        provider = TwoStageVision()
        self.extractor.provider = provider
        provider.responses = [self.empty_response, self.empty_response]
        for _ in range(2):
            with self.assertRaises(ValidationError): self.extractor.run(self.run)
        self.assertEqual((len(provider.observed), len(provider.calls)), (1, 2))

    def test_two_stage_empty_observations_never_reach_card_generation(self):
        provider = TwoStageVision()
        self.extractor.provider = provider
        provider.observation["observations"] = ""
        for _ in range(2):
            with self.assertRaises(ValidationError): self.extractor.run(self.run)
        self.assertEqual((len(provider.observed), len(provider.calls)), (2, 0))

    def test_two_stage_no_event_stops_before_card_generation(self):
        provider = TwoStageVision()
        self.extractor.provider = provider
        provider.observation = {"status": "no_event", "issues": ["没有可记录事件"], "observations": ""}
        self.assertEqual(self.extractor.run(self.run)["status"], "no_event")
        self.assertFalse(provider.calls)

    def test_completed_resume_needs_no_source_or_provider(self):
        result = self.extractor.run(self.run)
        self.source.unlink()
        Path(result["event_card"]).unlink()
        self.extractor.provider = None
        self.assertTrue(Path(self.extractor.run(self.run)["event_card"]).is_file())
        self.assertEqual(len(self.provider.calls), 1)

    def test_changed_source_blocks_before_request(self):
        self.source.write_bytes(b"changed")
        with self.assertRaises(ValidationError):
            self.extractor.run(self.run)
        self.assertFalse(self.provider.calls)

    def test_chunks_offset_times_and_resume_reuses_successful_calls(self):
        self.config["extraction"]["chunk_seconds"] = 3
        run = self.extractor.create(self.source, self.config)
        def first(context):
            return {"content": json.dumps({"status": "ready", "issues": [], "card": card_for(context)}), "finish_reason": "stop", "usage": {}}
        self.provider.responses = [first, ProviderError("offline interruption")]
        with self.assertRaises(ProviderError):
            self.extractor.run(run)
        self.assertEqual(self.store.load(run)["status"], "interrupted_or_failed")
        result = self.extractor.run(run)
        self.assertEqual(len(self.provider.calls), 4)  # first, failed second, second, merge
        card = read_json(result["event_card"])
        self.assertEqual([f["timestamp_seconds"] for f in card["key_frames"]], [1, 4])
        self.assertEqual(card["event_description"]["sequence"][1]["source_refs"][0]["start_seconds"], 3)
        self.assertEqual(self.prepare.call_count, 2)

    def test_bad_model_json_repaired_once(self):
        self.provider.responses = [{"content": "bad json", "finish_reason": "stop", "usage": {}}]
        self.extractor.run(self.run)
        self.assertEqual(len(self.provider.calls), 2)
        self.assertIn("repair", self.provider.calls[-1][0])

    def boundary_response(self, context):
        card = card_for(context)
        card["key_frames"][0]["timestamp_seconds"] = context["duration_seconds"]
        return {"content": json.dumps({"status": "ready", "issues": [], "card": card}), "finish_reason": "stop", "usage": {}}

    def empty_response(self, context):
        card = card_for(context)
        card["event_description"]["sequence"] = [{}]
        return {"content": json.dumps({"status": "ready", "issues": [], "card": card}), "finish_reason": "stop", "usage": {}}

    def test_boundary_repair_preserves_full_card_and_names_exact_field(self):
        self.provider.responses = [self.boundary_response]
        result = self.extractor.run(self.run)
        self.assertEqual(result["status"], "completed")
        repair = self.provider.calls[1][0]["repair"]
        self.assertEqual(repair["field"], "$.card.key_frames[0].timestamp_seconds")
        self.assertIn("< 6", repair["expected"])
        self.assertIn("不能等于", repair["expected"])
        original = json.loads(repair["previous_output"])
        self.assertEqual(original["card"]["key_frames"][0]["timestamp_seconds"], 6)
        self.assertIn("禁止用{}", repair["instruction"])

    def test_empty_repair_reports_nested_missing_fields_and_keeps_budget(self):
        self.provider.responses = [self.boundary_response, self.empty_response]
        for _ in range(2):
            with self.assertRaises(ValidationError) as error:
                self.extractor.run(self.run)
            self.assertIn("$.card.event_description.sequence[0]", str(error.exception))
            self.assertIn("source_refs", str(error.exception))
        self.assertEqual(len(self.provider.calls), 2)

    def test_old_boundary_failure_gets_one_versioned_repair_without_overwriting_responses(self):
        self.provider.responses = [self.boundary_response, self.empty_response]
        with self.assertRaises(ValidationError):
            self.extractor.run(self.run)
        root = self.store.path(self.run) / "stages/part-001"
        originals = {}
        for number in (1, 2):
            path = root / f"response-{number}.json"
            response = read_json(path)
            del response["validation_version"]  # Simulate a pre-fix task.
            write_json(path, response)
            originals[path] = path.read_bytes()
        self.assertEqual(self.extractor.run(self.run)["status"], "completed")
        self.assertEqual(len(self.provider.calls), 3)
        repair = self.provider.calls[-1][0]["repair"]
        self.assertEqual(json.loads(repair["previous_output"])["card"]["event_description"]["sequence"][0]["order"], 1)
        self.assertTrue((root / "response-repair-v2.json").exists())
        for path, data in originals.items():
            self.assertEqual(path.read_bytes(), data)

    def test_failed_legacy_repair_cannot_repeat_on_resume(self):
        self.provider.responses = [self.boundary_response, self.empty_response, self.empty_response]
        with self.assertRaises(ValidationError):
            self.extractor.run(self.run)
        root = self.store.path(self.run) / "stages/part-001"
        for number in (1, 2):
            path = root / f"response-{number}.json"
            response = read_json(path)
            del response["validation_version"]
            write_json(path, response)
        for _ in range(2):
            with self.assertRaises(ValidationError):
                self.extractor.run(self.run)
        self.assertEqual(len(self.provider.calls), 3)

    def test_failed_repair_budget_survives_resume(self):
        self.provider.responses = [{"content": "{}", "finish_reason": "stop", "usage": {}}] * 2
        for _ in range(2):
            with self.assertRaises(ValidationError):
                self.extractor.run(self.run)
        self.assertEqual(len(self.provider.calls), 2)

    def seed_old_empty_responses(self):
        self.provider.responses = [self.empty_response, self.empty_response]
        with self.assertRaises(ValidationError):
            self.extractor.run(self.run)
        root = self.store.path(self.run) / "stages/part-001"
        originals = {}
        for number in (1, 2):
            path = root / f"response-{number}.json"
            response = read_json(path)
            response["validation_version"] = 2
            write_json(path, response)
            originals[path] = path.read_bytes()
        return root, originals

    def test_legacy_empty_objects_get_one_schema_visible_repair(self):
        root, originals = self.seed_old_empty_responses()
        result = self.extractor.run(self.run)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(self.provider.calls), 3)
        self.assertEqual(read_json(root / "response-repair-v3.json")["validation_version"], 3)
        self.assertIn("source_refs", self.provider.calls[-1][0]["repair"]["expected"])
        for path, data in originals.items():
            self.assertEqual(path.read_bytes(), data)
        self.extractor.run(self.run)
        self.assertEqual(len(self.provider.calls), 3)

    def test_schema_migration_cannot_retry_indefinitely(self):
        root, _ = self.seed_old_empty_responses()
        self.provider.responses = [self.empty_response]
        for _ in range(2):
            with self.assertRaises(ValidationError):
                self.extractor.run(self.run)
        self.assertEqual(len(self.provider.calls), 3)
        self.assertFalse((root / "result.json").exists())

    def test_no_event_and_multi_event_do_not_export_empty_cards(self):
        for status in ("no_event", "needs_resolution"):
            with self.subTest(status=status):
                run = self.extractor.create(self.source, self.config)
                self.provider.responses = [{"content": json.dumps({"status": status, "issues": ["没有可整理的单一事件。"], "card": None}), "finish_reason": "stop", "usage": {}}]
                self.assertEqual(self.extractor.run(run)["status"], status)
                self.assertFalse((self.store.path(run) / "card.json").exists())

    def test_truncated_output_is_not_accepted(self):
        self.provider.responses = [{"content": "{}", "finish_reason": "length", "usage": {}}]
        self.extractor.run(self.run)
        self.assertIn("未完整结束", self.provider.calls[1][0]["repair"]["error"])

    def test_saved_prompt_and_schema_are_used_on_resume(self):
        (self.root / "prompts/event_extraction.md").write_text("new prompt", encoding="utf-8")
        self.extractor.run(self.run)
        self.assertEqual(len(self.provider.calls), 1)

    def test_frozen_model_is_required(self):
        self.provider.model = "different-model"
        with self.assertRaises(ValidationError):
            self.extractor.run(self.run)
        self.assertFalse(self.provider.calls)

    def test_tampered_completed_card_is_not_exported(self):
        self.extractor.run(self.run)
        card_path = self.store.path(self.run) / "card.json"
        card = read_json(card_path)
        card["basic_info"]["title"] = "changed"
        write_json(card_path, card)
        with self.assertRaises(ValidationError):
            self.extractor.run(self.run)


class CardValidationTests(unittest.TestCase):
    def setUp(self):
        self.schema = read_json(ROOT / "schemas/life_event_card.schema.json")
        self.context = {"event_id": "event_test", "material_id": "material_test", "duration_seconds": 6,
                        "allowed_frame_ids": ["frame_001"]}
        self.card = card_for(self.context)

    def test_invalid_facts_and_references_rejected(self):
        mutations = [
            lambda c: c["basic_info"].update(event_id="invented"),
            lambda c: c["basic_info"]["time_range"].update(start="2026-09-23T00:00:00Z"),
            lambda c: c["key_frames"][0].update(image_uri="fake.jpg"),
            lambda c: c["key_frames"][0].update(timestamp_seconds=6),
            lambda c: c["key_frames"][0].update(timestamp_seconds=float("nan")),
            lambda c: c["key_frames"][0].update(material_id="other"),
            lambda c: c["details_to_preserve"][0].update(frame_ids=["missing"]),
            lambda c: c["details_to_preserve"][0].update(frame_ids=[], sequence_orders=[]),
            lambda c: c["event_description"]["sequence"][0].update(order=True),
            lambda c: c["event_description"]["sequence"][0]["source_refs"][0].update(end_seconds=7),
            lambda c: c["selection_info"].update(is_duplicate=False),
            lambda c: c["memory_links"].update(character_ids=["made-up"]),
            lambda c: c.update(unexpected="extra"),
        ]
        for index, mutate in enumerate(mutations):
            with self.subTest(index=index):
                card = deepcopy(self.card)
                mutate(card)
                with self.assertRaises(ValidationError):
                    validate_card(card, self.schema, self.context)

    def test_audio_content_rejected_even_if_well_formed(self):
        self.card["audio_info"] = [{"audio_id": "a", "type": "action", "description": "猜测脚步声", "transcript": None, "source_refs": []}]
        with self.assertRaises(ValidationError):
            validate_card(self.card, self.schema, self.context)

    def test_nullable_card_reports_inner_object_error_and_accepts_valid_null(self):
        self.card["key_frames"][0] = {}
        with self.assertRaises(FieldValidationError) as error:
            validate_response({"status": "ready", "issues": [], "card": self.card}, self.schema, self.context)
        self.assertEqual(error.exception.field, "$.card.key_frames[0]")
        self.assertIn("timestamp_seconds", error.exception.expected)
        validate_response({"status": "no_event", "issues": ["无可记录画面"], "card": None}, self.schema, self.context)

    def test_timestamp_boundary_is_strict_while_interval_end_is_inclusive(self):
        self.card["key_frames"][0]["timestamp_seconds"] = 5.999
        validate_card(self.card, self.schema, self.context)
        self.card["key_frames"][0]["timestamp_seconds"] = 6
        with self.assertRaises(FieldValidationError) as error:
            validate_card(self.card, self.schema, self.context)
        self.assertEqual(error.exception.field, "$.card.key_frames[0].timestamp_seconds")

    def test_merge_cannot_move_frames_or_expand_evidence(self):
        for extra in ({"known_frames": {"frame_001": 2}}, {"allowed_ranges": [[0, 3]]}):
            with self.subTest(extra=extra), self.assertRaises(ValidationError):
                validate_card(self.card, self.schema, {**self.context, **extra})


class VisionProviderTests(unittest.TestCase):
    def test_actual_video_bytes_and_visible_schema_go_to_multimodal_api(self):
        credentials = Credentials("secret", "https://dashscope.aliyuncs.com/compatible-mode/v1", "https://dashscope.aliyuncs.com/api/v1")
        transport = Mock()
        transport.request.return_value = {"choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}], "usage": {}}
        provider = QwenVision(credentials, transport=transport)
        with tempfile.TemporaryDirectory() as directory:
            video = Path(directory) / "sample.mp4"
            video.write_bytes(b"video")
            schema = read_json(ROOT / "schemas/life_event_card.schema.json")
            provider.generate("rules", {"material_id": "m"}, schema, video=video)
        args = transport.request.call_args.args
        self.assertTrue(args[1].endswith("/chat/completions"))
        payload = args[2]
        self.assertEqual(payload["response_format"], {"type": "json_object"})
        self.assertIn(json.dumps(schema, ensure_ascii=False, separators=(",", ":")), payload["messages"][0]["content"])
        self.assertEqual(payload["messages"][1]["content"][0]["video_url"]["url"], "data:video/mp4;base64,dmlkZW8=")
        self.assertFalse(payload["enable_thinking"])
        self.assertIn("timestamp_seconds < context.duration_seconds", payload["messages"][0]["content"])
        self.assertIn("repair.field", payload["messages"][0]["content"])

    def test_text_only_merge_retains_strict_schema(self):
        credentials = Credentials("secret", "https://dashscope.aliyuncs.com/compatible-mode/v1", "https://dashscope.aliyuncs.com/api/v1")
        transport = Mock()
        transport.request.return_value = {"choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}]}
        schema = {"type": "object"}
        QwenVision(credentials, transport=transport).generate("rules", {"segment_cards": []}, schema)
        payload = transport.request.call_args.args[2]
        self.assertEqual(payload["response_format"]["type"], "json_schema")
        self.assertTrue(payload["response_format"]["json_schema"]["strict"])
        self.assertEqual(payload["response_format"]["json_schema"]["schema"], schema)


class ExtractionMediaTests(unittest.TestCase):
    def test_chunking_never_creates_sub_two_second_tail(self):
        ranges = chunk_ranges(60.2, 60)
        self.assertEqual(ranges[0][0], 0)
        self.assertEqual(ranges[-1][1], 60.2)
        self.assertTrue(all(end - start >= 2 for start, end in ranges))

    def test_real_transcode_has_no_audio_and_preserves_requested_time(self):
        media = Media(ROOT)
        if not media.ffmpeg or not media.ffprobe:
            self.skipTest("FFmpeg unavailable")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, target = root / "source.mp4", root / "chunk.mp4"
            media.execute([media.ffmpeg, "-nostdin", "-v", "error", "-f", "lavfi", "-i", "color=c=red:s=320x180:r=12:d=3",
                           "-f", "lavfi", "-i", "color=c=blue:s=320x180:r=12:d=3",
                           "-f", "lavfi", "-i", "sine=frequency=1000:duration=6",
                           "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0[v]", "-map", "[v]", "-map", "2:a",
                           "-t", "6", "-c:v", "libx264", "-threads", "2", "-c:a", "aac", str(source)])
            info = inspect_video(media, source)
            self.assertTrue(info["has_audio"])
            prepare_chunk(media, source, target, info, 3, 6)
            output = inspect_video(media, target)
            self.assertFalse(output["has_audio"])
            self.assertAlmostEqual(output["duration_seconds"], 3, delta=0.1)
            self.assertLess(target.stat().st_size, MAX_VIDEO_BYTES)
            # The second source interval must contain blue, not the first red interval.
            pixels = subprocess.run([media.ffmpeg, "-v", "error", "-i", str(target), "-vf", "scale=1:1", "-frames:v", "1",
                                     "-pix_fmt", "rgb24", "-f", "rawvideo", "-"], capture_output=True, check=True).stdout
            self.assertGreater(pixels[2], 180)
            self.assertLess(pixels[0], 70)


class ExtractionCliTests(unittest.TestCase):
    def test_extract_requires_exactly_one_input(self):
        with patch("sys.stderr", new_callable=io.StringIO):
            for flags in ([], ["--video", "a.mp4", "--run", "id"]):
                with self.assertRaises(SystemExit):
                    cli.parser().parse_args(["extract", *flags])

    def test_non_ready_extraction_has_nonzero_exit_code(self):
        with patch.object(cli, "run_command", return_value={"status": "no_event"}), patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(cli.main(["extract", "--video", "a.mp4"]), 2)


if __name__ == "__main__":
    unittest.main()
