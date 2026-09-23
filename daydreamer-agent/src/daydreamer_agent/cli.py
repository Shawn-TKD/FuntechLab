import argparse
import json
from pathlib import Path
import sys

from daydreamer_agent.application.pipeline import Pipeline
from daydreamer_agent.application.settings import Credentials, load_settings
from daydreamer_agent.domain.errors import AgentError, ValidationError
from daydreamer_agent.domain.events import normalize
from daydreamer_agent.media.ffmpeg import Media
from daydreamer_agent.memory.repository import load_memory, narrative_memory
from daydreamer_agent.providers.wan import WanVideo
from daydreamer_agent.providers.qwen import Qwen
from daydreamer_agent.prompts.video_compiler import RATIOS
from daydreamer_agent.storage.files import read_json, write_json
from daydreamer_agent.storage.jobs_sqlite import JobStore
from daydreamer_agent.story.generator import load_skill

ROOT = Path(__file__).resolve().parents[2]


def parser():
    p = argparse.ArgumentParser(description="白日梦想家：故事创作、视频生成与本地合成")
    p.add_argument("--project", type=Path, default=ROOT, help="项目目录")
    p.add_argument("--credentials-csv", type=Path, help="只在内存中读取百炼导出的密钥 CSV")
    commands = p.add_subparsers(dest="command", required=True)
    commands.add_parser("doctor", help="检查配置、Python 和本地媒体工具，不调用模型")
    extract = commands.add_parser("extract", help="理解本地视频，生成生活事件卡；不分析声音、不提取图片")
    extract_input = extract.add_mutually_exclusive_group(required=True)
    extract_input.add_argument("--video", type=Path, help="本地素材视频")
    extract_input.add_argument("--run", help="恢复事件卡提取任务")
    status = commands.add_parser("status", help="查看任务及镜头状态")
    status.add_argument("--run")
    for name in ("plan", "import-story", "run"):
        sub = commands.add_parser(name, help={"plan": "创作故事", "import-story": "将修改过的故事导入为独立新任务", "run": "一键创作、生成视频、拼接并导出，也可恢复任务"}[name])
        if name == "run":
            source = sub.add_mutually_exclusive_group()
            source.add_argument("--events", type=Path, help="从已有生活事件卡生成视频")
            source.add_argument("--video", type=Path, help="理解原始视频后自动生成事件卡、故事和短片")
        else:
            sub.add_argument("--events", type=Path)
        sub.add_argument("--memory", type=Path)
        sub.add_argument("--world-id")
        sub.add_argument("--duration", type=int)
        sub.add_argument("--ratio")
        sub.add_argument("--resolution")
        sub.add_argument("--continuity", choices=("single_take", "frame_chain"))
        if name in {"plan", "run"}:
            sub.add_argument("--run", help="恢复视频全流程或已有生成任务" if name == "run" else "恢复已有故事任务")
        else:
            sub.add_argument("--story", type=Path, required=True)
        if name == "run":
            sub.add_argument("--audio", type=Path, help="可选本地音乐音效清单；缺省生成无声预览")
            sub.add_argument("--wait", type=int, default=3600, help="视频阶段本次最长等待秒数，默认3600；超时可恢复")
    demo = commands.add_parser("demo", help="离线示例；不调用模型")
    demo.add_argument("--with-media", action="store_true", help="用测试画面和测试音调验证完整合成")
    render = commands.add_parser("render", help="提交或恢复付费视频任务，含有上限的超时重提")
    render.add_argument("--run", required=True)
    render.add_argument("--wait", type=int, default=1800, help="本次最多等待多少秒，默认1800；0为一次检查，之后可恢复")
    retry = commands.add_parser("retry-shot", help="为已明确失败的镜头准备新尝试，不立即提交")
    retry.add_argument("--run", required=True)
    retry.add_argument("--shot", required=True)
    regenerate = commands.add_parser("regenerate-shot", help="为已完成或失败的镜头及其后续片段准备重做，不立即提交")
    regenerate.add_argument("--run", required=True)
    regenerate.add_argument("--shot", required=True)
    attach = commands.add_parser("attach-task", help="核对平台后，为未知提交补录原任务编号")
    attach.add_argument("--run", required=True)
    attach.add_argument("--shot", required=True)
    attach.add_argument("--task-id", required=True)
    compose = commands.add_parser("compose", help="拼接本地镜头并混合指定音乐和音效")
    compose.add_argument("--run", required=True)
    group = compose.add_mutually_exclusive_group()
    group.add_argument("--audio", type=Path, help="音频清单 JSON")
    group.add_argument("--preview", action="store_true", help="只生成无声预览")
    return p


def new_run(args, config, store, *, demo=False):
    if not args.events:
        raise ValidationError("创建任务需要 --events。")
    raw = read_json(args.events)
    memory = load_memory(args.project / config["storage"]["memory_directory"], args.world_id or config["memory"]["world_id"], args.memory)
    production = config["production"]
    inputs = normalize(raw, memory=memory, duration=args.duration, ratio=args.ratio, resolution=args.resolution)
    constraints = inputs["production_constraints"]
    mode = args.continuity or constraints.get("continuity_mode", config["video"].get("continuity_mode", "frame_chain"))
    if mode not in {"single_take", "frame_chain"}:
        raise ValidationError("continuity_mode 必须为 single_take 或 frame_chain。")
    constraints["continuity_mode"] = mode
    for name, default in (("duration_seconds", production["duration_seconds"]), ("aspect_ratio", config["video"]["aspect_ratio"]), ("resolution", config["video"]["resolution"])):
        if constraints.get(name) is None and default != "":
            constraints[name] = default
    if getattr(args, "command", None) == "run":
        defaults = config.get("workflow", {})
        for name, default in (("duration_seconds", 12), ("aspect_ratio", "16:9"), ("resolution", "720P")):
            if constraints.get(name) is None:
                constraints[name] = defaults.get(name, default)
        if type(constraints["duration_seconds"]) is not int or constraints["duration_seconds"] < 3:
            raise ValidationError("一键生成的总时长必须为至少3秒的整数。")
        if constraints["aspect_ratio"] not in RATIOS or constraints["resolution"] not in {"480P", "720P", "1080P"}:
            raise ValidationError("一键生成需要有效画幅和分辨率。")
    # Revalidate defaults too; no free-form configuration bypasses input validation.
    inputs = normalize(inputs)
    if mode == "single_take" and constraints.get("duration_seconds") is not None and not 3 <= constraints["duration_seconds"] <= 15:
        raise ValidationError("当前文生视频连续模式只支持3–15秒；更长的连续视频需启用首帧续接。")
    original_memory = inputs.get("story_memory")
    inputs["story_memory"] = narrative_memory(original_memory)
    skill = load_skill(args.project / config["story"]["skill_directory"])
    run_id = store.create(inputs, config, demo=demo)
    path = store.path(run_id)
    write_json(path / "input/original.json", raw)
    write_json(path / "input/memory.json", original_memory)
    write_json(path / "input/skill.json", skill)
    if not demo and getattr(args, "command", None) in {"plan", "run"}:
        from daydreamer_agent.story.creativity import initialize
        initialize(store, run_id, config)
    print("任务已创建：" + run_id, flush=True)
    return run_id


def run_generation(args, config, store, media, credentials, run_id):
    from daydreamer_agent.application.delivery import export_delivery
    pipeline = Pipeline(store, media=media, progress=lambda message: print(message, flush=True))
    frozen = read_json(store.path(run_id) / "input/config.json")
    pipeline.story_provider = Qwen(credentials, frozen["story"]["model"], timeout=frozen["story"].get("request_timeout_seconds", 600))
    pipeline.video_provider = WanVideo(credentials)
    result = pipeline.run_full(run_id, audio_manifest=args.audio, wait_seconds=args.wait)
    if result["status"] in {"preview_ready", "completed"}:
        target = args.project / frozen["storage"].get("delivery_directory", "outputs") / run_id
        result["delivery"] = export_delivery(store.path(run_id), target, preview=result["status"] == "preview_ready")
        source = store.path(run_id) / "input/source-extraction.json"
        if source.exists():
            write_json(target / "生活事件卡.json", read_json(store.path(run_id) / "input/original.json"))
            write_json(target / "素材提取关联.json", read_json(source))
        print("视频已保存：" + result["delivery"]["video"], flush=True)
    else:
        print("流程尚未完成，进度已保存；恢复时使用上面的任务编号。", flush=True)
    return result


def run_command(args):
    args.project = args.project.resolve()
    config = load_settings(args.project)
    if args.credentials_csv is None:
        configured_csv = config.get("credentials", {}).get("csv_path")
        if configured_csv:
            candidate = args.project / configured_csv
            if candidate.is_file():
                args.credentials_csv = candidate
    media = Media(args.project)
    if args.command == "doctor":
        return {"python": sys.version.split()[0], "virtual_environment": sys.prefix != sys.base_prefix,
                "ffmpeg": media.ffmpeg, "ffprobe": media.ffprobe,
                "story_model": config["story"]["model"], "video_model": config["video"]["model"],
                "continuation_model": config["video"]["continuation_model"], "continuity_mode": config["video"]["continuity_mode"],
                "media_content_check": False, "network_called": False}
    if args.command == "extract":
        from daydreamer_agent.event_extraction.pipeline import EventExtraction
        from daydreamer_agent.providers.qwen_vision import QwenVision
        store = JobStore(args.project / "data/extractions", args.project / "data/extractions.sqlite3")
        extractor = EventExtraction(args.project, store, media)
        media.require_tools()
        run_id = args.run or extractor.create(args.video, config)
        print("事件卡提取任务：" + run_id, flush=True)
        print("恢复命令：daydreamer.cmd -Extract -Run " + run_id, flush=True)
        if store.load(run_id)["status"] != "completed":
            frozen = read_json(store.path(run_id) / "input/normalized.json")
            extractor.provider = QwenVision(Credentials.load(args.project, args.credentials_csv), frozen["settings"]["model"])
        result = extractor.run(run_id)
        if result["status"] == "completed":
            print("生活事件卡已保存：" + result["event_card"], flush=True)
        else:
            print("本次未生成事件卡：" + "；".join(result.get("issues", [])), flush=True)
        return result
    store = JobStore(args.project / config["storage"]["run_directory"], args.project / config["storage"]["task_database"])
    pipeline = Pipeline(store, media=media, progress=lambda message: print(message, flush=True))
    if args.command == "status":
        if not args.run:
            return [{"run_id": j["run_id"], "status": j["status"], "demo": j.get("demo", False)} for j in store.list()]
        job = store.load(args.run)
        job["shots"] = [read_json(p) for p in sorted((store.path(args.run) / "shots").glob("*/state.json"))]
        return job
    if args.command == "demo":
        from daydreamer_agent.demo import demo
        return demo(args.project, config, store, pipeline, with_media=args.with_media)
    if args.command == "run":
        if not 0 <= args.wait <= 86400:
            raise ValidationError("一键流程 --wait 必须在0–86400秒之间。")
        if args.run and any((args.events, args.video, args.memory, args.world_id, args.duration is not None, args.ratio, args.resolution, args.continuity)):
            raise ValidationError("恢复使用原任务输入；不要同时指定事件卡或制作参数。")
        if args.audio and not args.audio.is_file():
            raise ValidationError("找不到音频清单。")
        if args.video:
            from daydreamer_agent.application.video_workflow import validate_source_options
            validate_source_options(args, config)
        media.require_tools()
        credentials = Credentials.load(args.project, args.credentials_csv)
        extraction_resume = args.run and not (store.path(args.run) / "job.json").exists() and (args.project / "data/extractions" / args.run / "job.json").is_file()
        if args.video or extraction_resume:
            from daydreamer_agent.application.video_workflow import run_video_workflow
            return run_video_workflow(args, config, store, media, credentials,
                                      create_generation=new_run, generate=run_generation)
        run_id = args.run or new_run(args, config, store)
        print("恢复命令：daydreamer.cmd -Run " + run_id, flush=True)
        return run_generation(args, config, store, media, credentials, run_id)
    if args.command == "plan":
        credentials = Credentials.load(args.project, args.credentials_csv)
        if args.run and any((args.events, args.memory, args.world_id, args.duration is not None, args.ratio, args.resolution, args.continuity)):
            raise ValidationError("恢复任务使用原输入；修改输入请新建任务。")
        run_id = args.run or new_run(args, config, store)
        frozen = read_json(store.path(run_id) / "input/config.json")
        pipeline.story_provider = Qwen(credentials, frozen["story"]["model"], timeout=frozen["story"].get("request_timeout_seconds", 600))
        return pipeline.plan(run_id)
    if args.command == "import-story":
        story = read_json(args.story)
        run_id = new_run(args, config, store)
        with store.lock(run_id):
            pipeline.finish_story(run_id, story)
            store.status(run_id, "story_ready", story_source="user_import", semantic_review="user_supplied")
        return store.load(run_id)
    if args.command == "render":
        if not 0 <= args.wait <= 3600:
            raise ValidationError("--wait 必须在 0–3600 秒之间。")
        pipeline.video_provider = WanVideo(Credentials.load(args.project, args.credentials_csv))
        return pipeline.render(args.run, wait_seconds=args.wait)
    if args.command in {"retry-shot", "regenerate-shot"}:
        pipeline.retry_shot(args.run, args.shot, regenerate=args.command == "regenerate-shot")
        return store.shot(args.run, args.shot)
    if args.command == "attach-task":
        pipeline.attach_task(args.run, args.shot, args.task_id)
        return store.shot(args.run, args.shot)
    if args.command == "compose":
        return pipeline.compose(args.run, args.audio, preview=args.preview)


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        result = run_command(args)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if args.command == "run" and result.get("status") not in {"preview_ready", "completed"}:
            return 2
        if args.command == "extract" and result.get("status") != "completed":
            return 2
        return 0
    except AgentError as exc:
        print("未完成：" + str(exc), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("已中断；已有检查点保留。可用原任务编号恢复。", file=sys.stderr)
        return 130
    except (OSError, ValueError, KeyError, TypeError) as exc:
        # Never echo raw network exceptions, credentials, or environment values.
        print("处理失败：" + type(exc).__name__ + "。请检查文件格式、路径与权限。", file=sys.stderr)
        return 1
