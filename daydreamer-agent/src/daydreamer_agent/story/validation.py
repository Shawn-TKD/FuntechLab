import math

from daydreamer_agent.domain.errors import ValidationError
from daydreamer_agent.domain.continuity import validate_continuity
from daydreamer_agent.storage.files import canonical, identifier

PROMPT_KEYS = ("景别", "构图", "运镜手法", "画面内容")
STYLE_KEYS = ("medium", "form_and_space", "palette", "materials", "lighting", "motion_character", "story_based_reason")


def require(condition, message):
    if not condition:
        raise ValidationError(message)


def obj(value, fields, name):
    require(isinstance(value, dict), f"{name} 必须是对象。")
    for key in fields:
        require(key in value, f"{name} 缺少 {key}。")


def text_fields(value, fields, name):
    obj(value, fields, name)
    for key in fields:
        require(isinstance(value[key], str) and bool(value[key].strip()), f"{name}.{key} 必须是非空文本。")


def refs(values, allowed, name, *, nonempty=True):
    require(isinstance(values, list) and (bool(values) or not nonempty), f"{name} 必须是引用数组。")
    require(all(isinstance(v, str) and v in allowed for v in values), f"{name} 包含未知引用。")


def validate_story(story, inputs):
    required = ("schema_version", "status", "source_event_ids", "memory_basis", "assumptions", "theme", "script", "visual_style", "audio_plan", "shots", "checks", "issues")
    obj(story, required, "story")
    require(story["schema_version"] == "1.0", "故事 schema_version 必须是 1.0。")
    require(story["status"] in {"ready", "wait_for_material", "needs_resolution"}, "故事状态无效。")
    for name in ("assumptions", "script", "shots", "checks", "issues"):
        require(isinstance(story[name], list), f"{name} 必须是数组。")
    if story["status"] != "ready":
        require(bool(story["issues"]), "等待或冲突状态必须说明 issues。")
        return
    require(not story["issues"], "ready 故事不能有未解决的 issues。")
    events = {card["event_id"]: card for card in inputs["event_cards"]}
    refs(story["source_event_ids"], events, "source_event_ids")
    theme = story["theme"]
    text_fields(theme, ("title", "statement", "core_rule", "development", "boundary", "contrast_and_interest"), "theme")
    require(isinstance(theme.get("reality_mapping"), list) and bool(theme["reality_mapping"]), "主题需要现实映射。")
    for mapping in theme["reality_mapping"]:
        text_fields(mapping, ("event_id", "real_detail", "fantasy_translation", "retained_feature"), "reality_mapping")
        require(mapping["event_id"] in events, "现实映射引用未知事件。")
        validate_sources(mapping.get("source_refs"), [mapping["event_id"]], events)
    require(bool(story["script"]), "脚本不能为空。")
    beats = set()
    for beat in story["script"]:
        text_fields(beat, ("beat_id", "first_person_action", "visible_change", "end_state"), "script")
        require(beat["beat_id"] not in beats, "beat_id 重复。")
        beats.add(beat["beat_id"])
        refs(beat.get("source_event_ids"), events, "script.source_event_ids")
    style = story["visual_style"]
    text_fields(style, STYLE_KEYS, "visual_style")
    for name in ("stable_constraints", "transitions"):
        require(isinstance(style.get(name), list), "视觉风格需要 " + name + " 数组。")
    text_fields(story["audio_plan"], ("music_direction", "speech_policy"), "audio_plan")
    require(isinstance(story["audio_plan"].get("sound_motifs"), list), "声音方案需要 sound_motifs 数组。")
    require(bool(story["shots"]), "分镜不能为空。")
    shot_ids, covered = set(), set()
    total = 0
    known_duration = True
    for shot in story["shots"]:
        text_fields(shot, ("shot_id", "start_state", "end_state"), "shot")
        identifier(shot["shot_id"])
        require(shot["shot_id"] not in shot_ids, "shot_id 重复。")
        shot_ids.add(shot["shot_id"])
        refs(shot.get("beat_ids"), beats, "shot.beat_ids")
        covered.update(shot["beat_ids"])
        refs(shot.get("source_event_ids"), events, "shot.source_event_ids")
        validate_sources(shot.get("source_refs"), shot["source_event_ids"], events)
        prompt = shot.get("prompt")
        text_fields(prompt, PROMPT_KEYS, "shot.prompt")
        require(set(prompt) == set(PROMPT_KEYS), "每镜 prompt 只能包含景别、构图、运镜手法、画面内容。")
        require("第一人称" in prompt["画面内容"], "每镜画面内容需明确第一人称。")
        require(not any("同上" in text for text in prompt.values()), "镜头提示词不得使用同上。")
        require(shot.get("duration_basis") in {"confirmed", "proposal", "unspecified"}, "duration_basis 无效。")
        duration = shot.get("duration_seconds")
        if duration is None:
            known_duration = False
            require(shot["duration_basis"] == "unspecified", "未指定时长的 basis 应为 unspecified。")
        else:
            require(type(duration) in (int, float) and math.isfinite(duration) and duration > 0, "镜头时长必须为正数。")
            require(type(duration) is int and 3 <= duration <= 15, "当前视频模型每镜必须是 3–15 秒整数；短总时长需合并剧情到更少镜头。")
            total += duration
        obj(shot.get("audio"), ("music", "sfx", "continuity"), "shot.audio")
        require("transition_to_next" in shot, "缺少镜头衔接描述。")
    require(covered == beats, "存在未被任何分镜覆盖的剧情段落。")
    mode = inputs.get("production_constraints", {}).get("continuity_mode")
    if mode in {"single_take", "frame_chain"}:
        validate_continuity(story["shots"])
        if mode == "single_take":
            require(len(story["shots"]) == 1, "单次文生视频模式只能有一个连续镜头。")
    require(story["shots"][-1]["transition_to_next"] is None, "最后一镜 transition_to_next 必须为 null。")
    for shot in story["shots"][:-1]:
        require(isinstance(shot["transition_to_next"], str) and bool(shot["transition_to_next"].strip()), "相邻镜头必须说明衔接。")
    target = inputs.get("production_constraints", {}).get("duration_seconds")
    if target is not None:
        require(known_duration and abs(total - target) < 0.01, "分镜总时长与制作约束不符。")
    memory = inputs.get("story_memory") or {}
    basis = story["memory_basis"]
    obj(basis, ("world_id", "version", "used_ids"), "memory_basis")
    require(basis["world_id"] == memory.get("world_id") and basis["version"] == memory.get("version"), "故事记忆版本不匹配。")
    refs(basis["used_ids"], collect_memory_ids(memory), "memory_basis.used_ids", nonempty=False)
    for check in story["checks"]:
        text_fields(check, ("name", "result", "note"), "checks")
        require(check["result"] in {"pass", "not_applicable"}, "故事存在未通过的检查。")
    for delta in story.get("world_delta_draft", []):
        obj(delta, ("change_type", "target_id", "description", "basis_beat_ids", "base_memory_version"), "world_delta_draft")
        refs(delta["basis_beat_ids"], beats, "world_delta_draft.basis_beat_ids")
        require(delta["base_memory_version"] == memory.get("version"), "记忆草案基线版本不匹配。")


def collect_memory_ids(value):
    result = set()
    if isinstance(value, dict):
        for key, child in value.items():
            if (key == "id" or key.endswith("_id")) and isinstance(child, str):
                result.add(child)
            result.update(collect_memory_ids(child))
    elif isinstance(value, list):
        for child in value:
            result.update(collect_memory_ids(child))
    return result


def validate_sources(sources, event_ids, events):
    require(isinstance(sources, list), "source_refs 必须是数组。")
    allowed = {canonical(ref) for event_id in event_ids for ref in events[event_id]["source_refs"]}
    # Full event cards also expose stable IDs for described frame/audio evidence.
    for event_id in event_ids:
        for collection, key in (("key_frames", "frame_id"), ("audio_info", "audio_id")):
            for item in events[event_id].get(collection, []):
                if isinstance(item, dict) and isinstance(item.get(key), str) and item[key]:
                    allowed.add(canonical({key: item[key]}))
                    if collection == "key_frames":
                        # A local image path is optional when the frame ID and source position identify it.
                        allowed.add(canonical({field: item[field] for field in ("frame_id", "material_id", "timestamp_seconds") if item.get(field) is not None}))
    require(all(canonical(ref) in allowed for ref in sources),
            "source_refs 包含输入中未提供的素材或引用格式不匹配；请从对应 input.event_cards[].source_refs 原样复制完整引用对象，保留 material_id 和时间字段，不要将对象简写为素材编号字符串。")
