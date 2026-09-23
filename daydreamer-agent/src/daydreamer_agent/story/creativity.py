"""Frozen random choices and a previous-work snapshot for new creative runs."""
import math
import re
import secrets
import unicodedata

from daydreamer_agent.domain.errors import ValidationError
from daydreamer_agent.storage.files import digest, file_hash, read_json, write_json


def policy(config):
    raw = config.get("creativity", {})
    result = {"enabled": raw.get("enabled", False), "candidate_count": raw.get("candidate_count", 3),
              "temperature": raw.get("temperature", 1.0), "review_temperature": raw.get("review_temperature", 0.2),
              "max_rewrites": raw.get("max_rewrites", 2), "text_similarity_threshold": raw.get("text_similarity_threshold", 0.85)}
    if type(result["enabled"]) is not bool:
        raise ValidationError("creativity.enabled必须是布尔值。")
    for key, low, high in (("candidate_count", 2, 5), ("max_rewrites", 0, 3)):
        if type(result[key]) is not int or not low <= result[key] <= high:
            raise ValidationError(f"creativity.{key}必须为{low}至{high}的整数。")
    for key in ("temperature", "review_temperature", "text_similarity_threshold"):
        value = result[key]
        if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value < 2:
            raise ValidationError("创作采样或相似度参数无效。")
    if not 0.5 <= result["text_similarity_threshold"] <= 1:
        raise ValidationError("文本相似度阈值必须在0.5至1之间。")
    return result


def summary(story):
    return {"theme": {key: story["theme"][key] for key in ("title", "statement", "core_rule", "development", "boundary")},
            "script": [{key: beat[key] for key in ("first_person_action", "visible_change", "end_state")} for beat in story["script"]],
            "visual_style": {key: story["visual_style"][key] for key in ("medium", "form_and_space", "palette", "materials", "lighting", "motion_character")}}


def previous_work(store, current_id):
    candidates, skipped = [], []
    for file in store.runs.glob("*/job.json"):
        if file.parent.name == current_id:
            continue
        try:
            job = read_json(file)
            if job.get("demo") or job.get("outputs_stale") or not (job.get("preview") or job.get("final")):
                continue
            candidates.append((job.get("work_ready_at") or job["created_at"], file.parent, job))
        except (OSError, ValueError, KeyError, ValidationError):
            skipped.append(file.parent.name)
    for _, path, job in sorted(candidates, key=lambda item: (item[0], item[1].name), reverse=True):
        try:
            story = read_json(path / "story/result.json")
            if digest(story) != job["story_digest"]:
                raise ValidationError("changed story")
            manifest = read_json(path / ("manifest.json" if job.get("final") else "composition/preview-manifest.json"))
            artifact = (path / manifest["final" if job.get("final") else "preview"]).resolve()
            if not artifact.is_relative_to(path.resolve()) or file_hash(artifact) != manifest["sha256"]:
                raise ValidationError("changed video")
            return {"run_id": job["run_id"], "story_digest": job["story_digest"], "summary": summary(story)}, skipped
        except (OSError, ValueError, KeyError, TypeError, ValidationError):
            skipped.append(path.name)
    return None, skipped


def initialize(store, run_id, config):
    settings = policy(config)
    if not settings["enabled"]:
        return
    write_json(store.path(run_id) / "input/creativity.json", {
        "version": 2, "mode": "seed_only", "seed": secrets.randbelow(2**31),
        "policy": {"enabled": True, "temperature": settings["temperature"]},
    })


def stage_seed(base, round_index, stage, attempt=0):
    return int(digest([base, round_index, stage, attempt])[:16], 16) % 2**31


def normalized(text):
    return re.sub(r"[\W_]+", "", unicodedata.normalize("NFKC", text).lower())


def component_text(name, value):
    if name == "theme":
        return " ".join(value[key] for key in ("core_rule", "development"))
    if name == "script":
        return " ".join(beat[key] for beat in value for key in ("visible_change", "end_state"))
    return " ".join(value[key] for key in ("medium", "form_and_space", "materials", "motion_character"))


def similarity(name, left, right):
    a, b = (normalized(component_text(name, value)) for value in (left, right))
    if a == b:
        return 1.0
    if min(len(a), len(b)) < 3:
        return 0.0
    first = {a[i:i + 3] for i in range(len(a) - 2)}
    second = {b[i:i + 3] for i in range(len(b) - 2)}
    return len(first & second) / len(first | second)


def verify_decision(path, story):
    if not (path / "input/creativity.json").exists():
        return
    if read_json(path / "input/creativity.json").get("mode") == "seed_only":
        return
    decision_path = path / "story/diversity/decision.json"
    if not decision_path.exists():
        raise ValidationError("创作差异检查尚未完成，不能准备视频任务。")
    decision = read_json(decision_path)
    if decision.get("status") != "approved" or decision.get("story_digest") != digest(story):
        raise ValidationError("创作差异检查未通过或与当前故事不匹配，不能生成视频。")
