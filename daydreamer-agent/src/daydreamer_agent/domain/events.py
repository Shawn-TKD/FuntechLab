from copy import deepcopy
import math

from daydreamer_agent.domain.errors import ValidationError


def adapt_card(card):
    """Accept the full life-card format without inventing or opening source media."""
    if not isinstance(card, dict) or "basic_info" not in card:
        return card
    basic, description = card["basic_info"], card.get("event_description")
    if not isinstance(basic, dict) or not isinstance(description, dict):
        raise ValidationError("完整事件卡需要 basic_info 和 event_description 对象。")
    result = deepcopy(card)
    sequence = description.get("sequence", [])
    frames, audio = card.get("key_frames", []), card.get("audio_info", [])
    details = card.get("details_to_preserve", [])
    for name, items in (("sequence", sequence), ("key_frames", frames), ("audio_info", audio), ("details_to_preserve", details)):
        if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
            raise ValidationError(name + " 必须是对象数组。")
    references = []
    for item in sequence + audio:
        refs = item.get("source_refs", [])
        if not isinstance(refs, list):
            raise ValidationError("source_refs 必须是数组。")
        for ref in refs:
            if ref not in references:
                references.append(deepcopy(ref))
    for frame in frames:
        ref = {key: frame[key] for key in ("frame_id", "material_id", "timestamp_seconds", "image_uri") if frame.get(key) is not None}
        if ref and ref not in references:
            references.append(ref)
    unknowns = deepcopy(card.get("unknowns", []))
    if not isinstance(unknowns, list):
        raise ValidationError("unknowns 必须是数组。")
    source_note = "本次只读取事件卡文字；未读取素材视频、图片或原始音轨，关键画面的文字描述不是已看过图片的证明。"
    if source_note not in unknowns:
        unknowns.append(source_note)
    result.update(event_id=basic.get("event_id"), summary=description.get("summary"),
                  ordered_actions=deepcopy(sequence), details=deepcopy(details) + deepcopy(frames),
                  source_refs=references, sound_clues=deepcopy(audio), unknowns=unknowns)
    return result


def normalize(raw, *, memory=None, duration=None, ratio=None, resolution=None):
    if isinstance(raw, dict) and "event_cards" not in raw and ("basic_info" in raw or "summary" in raw):
        raw = {"event_cards": [raw], **{key: raw[key] for key in ("story_memory", "production_constraints", "current_preferences") if key in raw}}
    if isinstance(raw, list):
        raw = {"event_cards": raw}
    if not isinstance(raw, dict) or not isinstance(raw.get("event_cards"), list):
        raise ValidationError("输入必须是事件卡数组，或包含 event_cards 数组的 JSON 对象。")
    result = deepcopy(raw)
    if not result["event_cards"]:
        raise ValidationError("至少需要一张生活事件卡。")
    ids = set()
    for i, original_card in enumerate(result["event_cards"], 1):
        card = adapt_card(original_card)
        result["event_cards"][i - 1] = card
        if not isinstance(card, dict) or not isinstance(card.get("summary"), str) or not card["summary"].strip():
            raise ValidationError(f"第 {i} 张事件卡需要非空 summary。")
        if not card.get("event_id"):
            card["event_id"] = f"local-event-{i}"
            card["id_basis"] = "local"
        event_id = card["event_id"]
        if not isinstance(event_id, str) or event_id in ids:
            raise ValidationError("event_id 必须是唯一字符串。")
        ids.add(event_id)
        for name in ("ordered_actions", "details", "source_refs", "sound_clues", "unknowns"):
            card.setdefault(name, [])
            if not isinstance(card[name], list):
                raise ValidationError(f"{event_id}.{name} 必须是数组。")
    result["story_memory"] = memory if memory is not None else result.get("story_memory")
    if result["story_memory"] is not None and not isinstance(result["story_memory"], dict):
        raise ValidationError("story_memory 必须是对象或 null。")
    constraints = result.setdefault("production_constraints", {})
    if not isinstance(constraints, dict):
        raise ValidationError("production_constraints 必须是对象。")
    for name, value in (("duration_seconds", duration), ("aspect_ratio", ratio), ("resolution", resolution)):
        if value is not None:
            constraints[name] = value
        else:
            constraints.setdefault(name, None)
    total = constraints["duration_seconds"]
    if total is not None and (type(total) not in (int, float) or not math.isfinite(total) or total <= 0):
        raise ValidationError("总时长必须是正数。")
    result.setdefault("current_preferences", [])
    return result
