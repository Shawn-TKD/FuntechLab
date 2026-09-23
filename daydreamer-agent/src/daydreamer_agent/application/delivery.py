"""Export verified local results with a readable storyboard."""
from pathlib import Path
import shutil

from daydreamer_agent.domain.errors import ValidationError
from daydreamer_agent.storage.files import file_hash, read_json, write_json


def export_delivery(run_path, output, *, preview=True):
    run_path, output = Path(run_path).resolve(), Path(output).resolve()
    manifest = read_json(run_path / ("composition/preview-manifest.json" if preview else "manifest.json"))
    source = (run_path / manifest["preview" if preview else "final"]).resolve()
    job = read_json(run_path / "job.json")
    if (not source.is_relative_to(run_path) or job.get("outputs_stale")
            or not source.is_file() or file_hash(source) != manifest["sha256"]):
        raise ValidationError("导出视频缺失、过期或已变化，请恢复原任务。")
    story = read_json(run_path / "story/result.json")
    from daydreamer_agent.story.creativity import verify_decision
    verify_decision(run_path, story)
    output.mkdir(parents=True, exist_ok=True)
    video = output / ("连续续接_无声预览.mp4" if preview else "成片.mp4")
    temporary = video.with_suffix(".part.mp4")
    shutil.copyfile(source, temporary)
    temporary.replace(video)
    story_path = output / "完整故事与分镜.json"
    write_json(story_path, story)
    decision = run_path / "story/diversity/decision.json"
    if decision.exists():
        write_json(output / "创作差异检查.json", read_json(decision))
    sampling = run_path / "input/creativity.json"
    if sampling.exists() and read_json(sampling).get("mode") == "seed_only":
        write_json(output / "创作随机种子.json", read_json(sampling))
        attempts = run_path / "story/creation-attempts.json"
        if attempts.exists():
            write_json(output / "创作重开记录.json", read_json(attempts))
    record = {**manifest, "story_source": job.get("story_source"), "status": job["status"],
              "exported_video": video.name, "continuity_verified": False}
    if preview:
        record["exported_preview"] = video.name
        record["audio"] = "未配置音乐和音效，保留无声预览"
    write_json(output / "本轮运行记录.json", record)
    duration = sum(shot["duration_seconds"] for shot in story["shots"])
    mode = "首段文生视频，后段以前段实际剪辑末帧续接。" if manifest["continuity_method"] == "frame_chain" else "按分镜顺序合成。"
    lines = ["# " + story["theme"]["title"], "", story["theme"]["statement"], "",
             f"{duration}秒" + ("无声预览；" if preview else "成片；") + mode, "",
             "已进行文件技术检查；未做媒体内容检查，尚未验证画面完全连贯。", "",
             "## 主题规则", "", story["theme"]["core_rule"], "", "## 视觉风格", ""]
    for key in ("medium", "form_and_space", "palette", "materials", "lighting", "motion_character"):
        lines.extend([story["visual_style"][key], ""])
    lines.extend(["## 片段脚本", ""])
    for beat in story["script"]:
        lines.extend([beat["first_person_action"], "", beat["visible_change"], ""])
    at = 0
    for index, shot in enumerate(story["shots"], 1):
        end = at + shot["duration_seconds"]
        lines.extend([f"## 片段{index}：{at}–{end}秒", ""])
        for label, value in shot["prompt"].items():
            lines.extend([f"**{label}**：{value}", ""])
        lines.extend(["结束状态：" + shot["end_state"], ""])
        at = end
    storyboard = output / "故事与分镜.md"
    storyboard.write_text("\n".join(lines), encoding="utf-8")
    return {"directory": str(output), "video": str(video), "story": str(story_path), "storyboard": str(storyboard)}
