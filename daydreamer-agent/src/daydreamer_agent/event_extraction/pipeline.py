from copy import deepcopy
import json
from pathlib import Path

from daydreamer_agent.domain.errors import FieldValidationError, ValidationError
from daydreamer_agent.event_extraction.media import chunk_ranges, extraction_settings, inspect_video, prepare_chunk
from daydreamer_agent.event_extraction.validation import response_schema, validate_card, validate_response
from daydreamer_agent.storage.files import digest, file_hash, read_json, write_json


class EventExtraction:
    def __init__(self, project, store, media, provider=None, progress=print):
        self.project, self.store, self.media = Path(project), store, media
        self.provider, self.progress = provider, progress

    def create(self, source, config):
        source = Path(source).resolve()
        settings = extraction_settings(config)
        material = inspect_video(self.media, source)
        if material["duration_seconds"] > settings["max_duration_seconds"]:
            raise ValidationError(f"当前提取入口最多处理{settings['max_duration_seconds']}秒视频，请截取事件片段后再试。")
        sha = file_hash(source)
        material.update(material_id="material_" + sha[:24], sha256=sha, file_path=str(source), original_filename=source.name)
        schema = read_json(self.project / "schemas/life_event_card.schema.json")
        prompt = (self.project / "prompts/event_extraction.md").read_text(encoding="utf-8")
        run_id = self.store.create({"material": material, "settings": settings}, config)
        path = self.store.path(run_id)
        write_json(path / "input/rules.json", {"schema": schema, "prompt": prompt})
        self.store.status(run_id, "created", kind="event_extraction", event_id="event_" + run_id,
                          audio_review_status="not_analyzed", key_frame_images_saved=False)
        return run_id

    def stage(self, run_id, name, context, rules, *, video=None, fps=2):
        """Persist responses before validation; only one repair, including across resumes."""
        path = self.store.path(run_id) / "stages" / name
        basis = digest({"rules": rules, "context": context, "video_sha256": file_hash(video) if video else None,
                        "model": self.provider.model, "fps": fps, "version": 1})
        checkpoint = path / "result.json"
        if checkpoint.exists():
            saved = read_json(checkpoint)
            if saved["basis"] != basis:
                raise ValidationError("已保存的提取输入发生变化，请创建新任务。")
            validate_response(saved["result"], rules["schema"], context)
            return saved["result"]
        repair = None
        legacy_boundary_repair = None
        legacy_second = False
        for attempt in (1, 2, 3):
            if attempt == 3:
                if not (legacy_boundary_repair and legacy_second):
                    break
                # One bounded migration for old exhausted endpoint failures.
                # Original responses remain untouched; reuse the complete first card.
                repair = legacy_boundary_repair
                self.progress("旧事件卡时间边界修正升级：保留首次完整内容，按具体字段重新核对一次。")
            response_path = path / (f"response-{attempt}.json" if attempt < 3 else "response-repair-v2.json")
            if response_path.exists():
                response = read_json(response_path)
                if response["basis"] != basis:
                    raise ValidationError("提取响应的输入指纹不匹配，请创建新任务。")
                if attempt == 2:
                    legacy_second = "validation_version" not in response
            else:
                self.progress("正在理解视频：" + name + ("（修正引用或格式）" if repair else ""))
                request_context = {**context, **({"repair": repair} if repair else {})}
                response = self.provider.generate(rules["prompt"], request_context, response_schema(rules["schema"]), video=video, fps=fps)
                response["basis"] = basis
                response["validation_version"] = 2
                write_json(response_path, response)
            try:
                if response["finish_reason"] != "stop":
                    raise ValidationError("模型输出未完整结束；请精简事件卡，避免截断。")
                result = json.loads(response["content"], parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
                validate_response(result, rules["schema"], context)
                write_json(checkpoint, {"basis": basis, "result": result})
                return result
            except (ValueError, ValidationError) as exc:
                message = str(exc) if isinstance(exc, ValidationError) else "模型返回的内容不是合法JSON。"
                repair = {"error": message, "previous_output": response["content"],
                          "instruction": "核对field和expected，保留其他有效内容，返回完整事件卡；禁止用{}省略数组条目，禁止只返回补丁。"}
                if isinstance(exc, FieldValidationError):
                    repair.update(field=exc.field, expected=exc.expected)
                    if (attempt == 1 and "validation_version" not in response
                            and exc.field.startswith("$.card.key_frames[") and exc.field.endswith(".timestamp_seconds")):
                        legacy_boundary_repair = deepcopy(repair)
                self.progress("事件卡校验未通过：" + message)
        raise ValidationError("事件卡修正后仍未通过检查：" + repair["error"])

    def run(self, run_id):
        with self.store.lock(run_id):
            path = self.store.path(run_id)
            job = self.store.load(run_id)
            if job.get("kind") != "event_extraction":
                raise ValidationError("该编号不是事件卡提取任务。")
            inputs = read_json(path / "input/normalized.json")
            material, settings = inputs["material"], inputs["settings"]
            rules = read_json(path / "input/rules.json")
            if job["status"] == "completed":
                card = read_json(path / "card.json")
                if digest(card) != job["card_digest"]:
                    raise ValidationError("已完成事件卡发生变化，无法作为原任务导出。")
                return self.export(run_id, card, material)
            source = Path(material["file_path"])
            if not source.is_file() or file_hash(source) != material["sha256"]:
                raise ValidationError("原视频不存在或已被修改，请用原素材恢复或创建新任务。")
            if self.provider is None or self.provider.model != settings["model"]:
                raise ValidationError("请使用任务保存的视频理解模型配置。")
            self.store.status(run_id, "extracting", issues=[])
            try:
                cards, observations = [], []
                for index, (start, end) in enumerate(chunk_ranges(material["duration_seconds"], settings["chunk_seconds"]), 1):
                    name = f"part-{index:03}"
                    clip = path / "media" / (name + ".mp4")
                    clip_meta = clip.with_suffix(".json")
                    valid = clip.is_file() and clip_meta.exists() and read_json(clip_meta).get("sha256") == file_hash(clip)
                    if not valid:
                        self.progress(f"准备无声视频片段 {index}：{start:.2f}–{end:.2f}秒")
                        prepare_chunk(self.media, source, clip, material, start, end)
                        write_json(clip_meta, {"sha256": file_hash(clip), "start_seconds": start, "end_seconds": end})
                    context = {"event_id": job["event_id"], "material_id": material["material_id"],
                               "duration_seconds": end - start, "time_basis": "当前视频片段内的秒数，从0开始，不加偏移量",
                               "allowed_frame_ids": [f"part_{index:03}_frame_{n:03}" for n in range(1, 25)],
                               "audio_analyzed": False, "recorded_at": None}
                    result = self.stage(run_id, name, context, rules, video=clip, fps=settings["fps"])
                    observations.append({"part": name, "start_seconds": start, "end_seconds": end,
                                         "status": result["status"], "issues": result["issues"]})
                    write_json(path / "observations.json", observations)
                    if result["status"] == "needs_resolution":
                        self.store.status(run_id, "needs_resolution", issues=result["issues"])
                        return self.store.load(run_id)
                    if result["status"] == "ready":
                        card = deepcopy(result["card"])
                        for frame in card["key_frames"]:
                            frame["timestamp_seconds"] += start
                        for step in card["event_description"]["sequence"]:
                            for ref in step["source_refs"]:
                                ref["start_seconds"] += start
                                ref["end_seconds"] = min(end, ref["end_seconds"] + start)
                        cards.append(card)
                if not cards:
                    issues = list(dict.fromkeys(issue for item in observations for issue in item["issues"]))
                    self.store.status(run_id, "no_event", issues=issues or ["已检查的视频片段没有可记录事件，未生成空事件卡。"])
                    return self.store.load(run_id)
                context = {"event_id": job["event_id"], "material_id": material["material_id"],
                           "duration_seconds": material["duration_seconds"], "time_basis": "原视频内的秒数，输入已完成偏移换算",
                           "allowed_frame_ids": [f["frame_id"] for c in cards for f in c["key_frames"]],
                           "known_frames": {f["frame_id"]: f["timestamp_seconds"] for c in cards for f in c["key_frames"]},
                           "allowed_ranges": [[r["start_seconds"], r["end_seconds"]] for c in cards for s in c["event_description"]["sequence"] for r in s["source_refs"]],
                           "segment_cards": cards, "segment_observations": observations,
                           "task": "将同一事件的连续片段合并成一张卡，重排动作顺序和细节引用；不同独立事件返回needs_resolution。"}
                if len(cards) == 1:
                    card = cards[0]
                else:
                    result = self.stage(run_id, "merge", context, rules, fps=settings["fps"])
                    if result["status"] != "ready":
                        self.store.status(run_id, result["status"], issues=result["issues"])
                        return self.store.load(run_id)
                    card = result["card"]
                validate_card(card, rules["schema"], context)
                # Detect source changes during processing rather than delivering mixed evidence.
                if file_hash(source) != material["sha256"]:
                    raise ValidationError("处理期间原视频发生变化，请创建新任务。")
                write_json(path / "card.json", card)
                self.store.status(run_id, "completed", card_digest=digest(card), issues=[])
            except (Exception, KeyboardInterrupt):
                self.store.status(run_id, "interrupted_or_failed")
                raise
            return self.export(run_id, card, material)

    def export(self, run_id, card, material):
        config = read_json(self.store.path(run_id) / "input/config.json")
        target = self.project / config["storage"].get("delivery_directory", "outputs") / "event-cards" / run_id
        target.mkdir(parents=True, exist_ok=True)
        card_path = target / (card["basic_info"]["event_id"] + ".json")
        write_json(card_path, card)
        write_json(target / "素材引用表.json", {"materials": [material]})
        job = self.store.load(run_id)
        write_json(target / "提取记录.json", {"run_id": run_id, "model": read_json(self.store.path(run_id) / "input/normalized.json")["settings"]["model"],
                   "status": job["status"], "card_sha256": file_hash(card_path), "audio_review_status": "not_analyzed",
                   "key_frame_images_saved": False, "timestamps": "模型估计的原视频秒数，经过范围校验，未进行逐帧核对",
                   "deduplication": "not_performed", "memory_linking": "not_performed", "factual_review": "not_manually_verified",
                   "segments": read_json(self.store.path(run_id) / "observations.json")})
        return {"run_id": run_id, "status": "completed", "event_card": str(card_path), "directory": str(target)}
