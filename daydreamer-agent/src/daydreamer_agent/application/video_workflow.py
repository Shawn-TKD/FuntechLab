"""Bridge event extraction to generation with a durable child-task link."""
from daydreamer_agent.providers.video_models import capabilities
from daydreamer_agent.application.settings import audio_policy
from copy import copy
from pathlib import Path

from daydreamer_agent.domain.errors import ValidationError
from daydreamer_agent.event_extraction.pipeline import EventExtraction
from daydreamer_agent.providers.qwen_vision import QwenVision
from daydreamer_agent.prompts.video_compiler import RATIOS
from daydreamer_agent.memory.repository import load_memory
from daydreamer_agent.storage.files import digest, read_json, write_json
from daydreamer_agent.storage.jobs_sqlite import JobStore
from daydreamer_agent.story.timing import validate_timing_constraints


def validate_source_options(args, config):
    """Reject unusable generation options before paying for video understanding."""
    defaults = config.get("workflow", {})
    audio_policy(config, "manual" if args.audio else getattr(args, "audio_mode", None))
    spec = capabilities(config["video"]["model"])
    duration = args.duration if args.duration is not None else (config["production"]["duration_seconds"] or defaults.get("duration_seconds", 12))
    duration = None if duration == "" else duration
    ratio = args.ratio or config["video"]["aspect_ratio"] or defaults.get("aspect_ratio", "16:9")
    resolution = args.resolution or config["video"]["resolution"] or defaults.get("resolution", "720P")
    mode = args.continuity or config["video"].get("continuity_mode", "frame_chain")
    if (duration is not None and (type(duration) is not int or duration < spec["minimum"])) or ratio not in RATIOS or resolution not in spec["resolutions"]:
        raise ValidationError("全流程制作参数无效，请检查时长、画幅和分辨率。")
    if mode not in {"frame_chain", "single_take"} or (mode == "single_take" and duration is not None and duration > 15):
        raise ValidationError("连续模式无效；single_take最多15秒。")
    from daydreamer_agent.story.pacing import freeze_policy
    constraints = {"video_model": config["video"]["model"], "duration_seconds": duration,
                   "max_duration_seconds": defaults.get("max_duration_seconds"), "continuity_mode": mode}
    freeze_policy(constraints, config)
    validate_timing_constraints(constraints)
    if duration is None and defaults.get("max_duration_seconds") is None:
        raise ValidationError("自动时长需要配置workflow.max_duration_seconds。")
    load_memory(args.project / config["storage"]["memory_directory"], args.world_id or config["memory"]["world_id"], args.memory)


def run_video_workflow(args, config, generation_store, media, credentials, *, create_generation, generate):
    extraction_store = JobStore(args.project / "data/extractions", args.project / "data/extractions.sqlite3")
    extractor = EventExtraction(args.project, extraction_store, media)
    run_id = args.run or extractor.create(args.video, config)
    path = extraction_store.path(run_id)
    print("全流程任务：" + run_id, flush=True)
    print("恢复命令：daydreamer.cmd -Run " + run_id, flush=True)
    with extraction_store.lock(run_id):
        job = extraction_store.load(run_id)
        if job.get("kind") != "event_extraction":
            raise ValidationError("该编号不是视频理解任务。")
        if "generation_options" not in job:
            options = {name: getattr(args, name) for name in ("world_id", "duration", "ratio", "resolution", "continuity")}
            options["audio_mode"] = getattr(args, "audio_mode", None)
            for name in ("memory", "audio"):
                value = getattr(args, name)
                options[name] = str(value.resolve()) if value else None
            job.update(generation_options=options, generation_status="pending")
            extraction_store.save(run_id, job)
    # A completed extraction can be re-exported without re-uploading its source.
    if job["status"] != "completed":
        settings = read_json(path / "input/normalized.json")["settings"]
        extractor.provider = QwenVision(credentials, settings["model"])
    extraction = extractor.run(run_id)
    if extraction["status"] != "completed":
        print("事件卡尚未准备好，后续创作和视频生成未启动。", flush=True)
        return {**extraction, "workflow_stage": "extraction"}
    print("生活事件卡已保存：" + extraction["event_card"], flush=True)

    # Link under the extraction lock before any paid creative/generation call.
    # A crash before this link can leave an unused child, but cannot submit its videos.
    with extraction_store.lock(run_id):
        job = extraction_store.load(run_id)
        card = read_json(path / "card.json")
        if digest(card) != job["card_digest"]:
            raise ValidationError("提取的事件卡已变化，不能接入原生成任务。")
        child_args = copy(args)
        child_args.video = None
        child_args.events = path / "card.json"
        for name, value in job["generation_options"].items():
            setattr(child_args, name, Path(value) if name in {"memory", "audio"} and value else value)
        # Audio may explicitly be supplied on a later resume; otherwise retain the original choice.
        if args.audio:
            child_args.audio = args.audio
            child_args.audio_mode = "manual"
        elif getattr(args, "audio_mode", None):
            child_args.audio_mode = args.audio_mode
            if args.audio_mode != "manual":
                child_args.audio = None
        if args.audio or getattr(args, "audio_mode", None):
            job["generation_options"].update(audio_mode=child_args.audio_mode,
                                              audio=str(child_args.audio.resolve()) if child_args.audio else None)
            extraction_store.save(run_id, job)
        frozen_config = read_json(path / "input/config.json")
        child_id = job.get("generation_run_id")
        if not child_id:
            child_id = create_generation(child_args, frozen_config, generation_store)
            write_json(generation_store.path(child_id) / "input/source-extraction.json", {
                "extraction_run_id": run_id, "card_digest": job["card_digest"],
                "generation_requested_by": "user_selected_video_for_full_workflow",
                "deduplication_performed": False,
            })
            job.update(generation_run_id=child_id, generation_status="created")
            extraction_store.save(run_id, job)
        source = read_json(generation_store.path(child_id) / "input/source-extraction.json")
        if (source["extraction_run_id"] != run_id or source["card_digest"] != job["card_digest"]
                or digest(read_json(generation_store.path(child_id) / "input/original.json")) != job["card_digest"]):
            raise ValidationError("生成任务与事件卡来源不匹配，停止以免混用结果。")
    print("自动进入故事与视频生成，生成任务：" + child_id, flush=True)
    result = generate(child_args, frozen_config, generation_store, media, credentials, child_id)
    with extraction_store.lock(run_id):
        job = extraction_store.load(run_id)
        job["generation_status"] = result["status"]
        extraction_store.save(run_id, job)
    return {**result, "run_id": run_id, "generation_run_id": child_id,
            "event_card": extraction["event_card"], "workflow_stage": "generation"}
