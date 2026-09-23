"""Validate the deliberately small JSON Schema subset used by the event card."""
import math

from daydreamer_agent.domain.errors import FieldValidationError, ValidationError


def matches_type(value, kind):
    return {"object": type(value) is dict, "array": type(value) is list,
            "string": type(value) is str, "null": value is None,
            "boolean": type(value) is bool, "integer": type(value) is int,
            "number": type(value) in (int, float) and math.isfinite(value)}[kind]


def validate_schema(value, schema, path="$"):
    if "anyOf" in schema:
        errors = []
        for branch in schema["anyOf"]:
            try:
                validate_schema(value, branch, path)
                return
            except ValidationError as exc:
                if branch.get("type") and matches_type(value, branch["type"]):
                    errors.append(exc)
        # A malformed card object should expose its missing nested fields, not
        # be replaced by the irrelevant error from the nullable alternative.
        if errors:
            raise errors[0]
        raise FieldValidationError(path, "不符合允许的类型：" + "、".join(branch.get("type", "组合类型") for branch in schema["anyOf"]) + "。")
    kind = schema["type"]
    valid = matches_type(value, kind)
    if not valid or ("enum" in schema and value not in schema["enum"]):
        raise FieldValidationError(path, f"必须为{kind}" + (f"，允许取值为{schema['enum']}" if "enum" in schema else "") + "。")
    if kind == "object":
        properties = schema["properties"]
        missing = sorted(set(schema["required"]) - value.keys())
        extra = sorted(value.keys() - properties.keys()) if schema.get("additionalProperties") is False else []
        if missing or extra:
            details = (["缺少必填字段：" + "、".join(missing)] if missing else []) + (["包含未定义字段：" + "、".join(extra)] if extra else [])
            raise FieldValidationError(path, "；".join(details) + "。必须返回完整对象，不能用{}省略条目。")
        for key, item in value.items():
            validate_schema(item, properties[key], path + "." + key)
    elif kind == "array":
        for index, item in enumerate(value):
            validate_schema(item, schema["items"], f"{path}[{index}]")


def nonempty(value, label):
    if not isinstance(value, str) or not value.strip() or value.strip().lower() in {"null", "无"}:
        raise ValidationError(label + " 需要有效文字。")


def validate_card(card, schema, context):
    validate_schema(card, schema, "$.card")
    basic = card["basic_info"]
    if basic["event_id"] != context["event_id"] or basic["time_range"] != {"start": None, "end": None}:
        raise ValidationError("事件ID或未确认的拍摄时间不正确。")
    for key in ("title", "scene"):
        nonempty(basic[key], key)
    nonempty(card["event_description"]["summary"], "summary")
    nonempty(card["selection_info"]["reason"], "reason")
    if card["audio_info"] or any(card["memory_links"].values()):
        raise ValidationError("本轮声音信息和记忆关联必须为空。")
    selection = card["selection_info"]
    if selection["is_duplicate"] is not None or selection["duplicate_of_event_id"] is not None or selection["status"] != "waiting_accumulation":
        raise ValidationError("本轮未进行去重；卡片状态必须为待积累。")
    duration, material = context["duration_seconds"], context["material_id"]
    sequence = card["event_description"]["sequence"]
    if [step["order"] for step in sequence] != list(range(1, len(sequence) + 1)):
        raise ValidationError("动作顺序必须从1开始连续递增。")
    previous_start = -1
    for step in sequence:
        nonempty(step["description"], "sequence.description")
        if not step["source_refs"]:
            raise ValidationError("每一步需要至少一条素材引用。")
        for ref in step["source_refs"]:
            if ref["material_id"] != material or not 0 <= ref["start_seconds"] < ref["end_seconds"] <= duration:
                raise ValidationError("步骤素材ID不符或时间范围超出素材。")
            if "allowed_ranges" in context and not any(lo <= ref["start_seconds"] < ref["end_seconds"] <= hi for lo, hi in context["allowed_ranges"]):
                raise ValidationError("合并后的步骤不能引用输入步骤以外的时间段；可保留多条来源引用。")
        start = min(ref["start_seconds"] for ref in step["source_refs"])
        if start < previous_start:
            raise ValidationError("动作描述必须按时间顺序排列。")
        previous_start = start
    frames = card["key_frames"]
    frame_ids = [frame["frame_id"] for frame in frames]
    if len(frame_ids) != len(set(frame_ids)) or not set(frame_ids) <= set(context["allowed_frame_ids"]):
        raise ValidationError("关键画面ID必须唯一，且来自程序提供的列表。")
    for index, frame in enumerate(frames):
        path = f"$.card.key_frames[{index}]"
        if frame["image_uri"] is not None:
            raise FieldValidationError(path + ".image_uri", "必须为null，关键画面只保留文字。")
        if frame["material_id"] != material:
            raise FieldValidationError(path + ".material_id", "必须使用context.material_id。")
        if not 0 <= frame["timestamp_seconds"] < duration:
            raise FieldValidationError(path + ".timestamp_seconds",
                                       f"当前值为{frame['timestamp_seconds']}；必须满足0 <= timestamp_seconds < {duration}秒，不能等于视频结束时间。请根据视频重新确认该画面位置，不能凭空改为任意较小数值。")
        nonempty(frame["description"], "key_frames.description")
        if not frame["reference_focus"]:
            raise ValidationError("关键画面需要参考说明。")
        for focus in frame["reference_focus"]:
            nonempty(focus, "reference_focus")
        known = context.get("known_frames", {})
        if known and (frame["frame_id"] not in known or frame["timestamp_seconds"] != known[frame["frame_id"]]):
            raise ValidationError("合并时不能新增或移动关键画面记录。")
    orders = {step["order"] for step in sequence}
    for detail in card["details_to_preserve"]:
        nonempty(detail["description"], "details.description")
        nonempty(detail["value"], "details.value")
        if (detail["audio_ids"] or not set(detail["frame_ids"]) <= set(frame_ids)
                or not set(detail["sequence_orders"]) <= orders
                or not (detail["frame_ids"] or detail["sequence_orders"])):
            raise ValidationError("保留细节必须引用本卡已有的画面或动作，不能引用声音。")
    if not sequence and not frames:
        raise ValidationError("有效事件需要至少一条动作或关键画面记录。")


def response_schema(card_schema):
    return {"type": "object", "properties": {
        "status": {"type": "string", "enum": ["ready", "no_event", "needs_resolution"]},
        "issues": {"type": "array", "items": {"type": "string"}},
        "card": {"anyOf": [card_schema, {"type": "null"}]},
    }, "required": ["status", "issues", "card"], "additionalProperties": False}


def validate_response(result, card_schema, context):
    validate_schema(result, response_schema(card_schema))
    if result["status"] == "ready":
        if result["issues"] or result["card"] is None:
            raise ValidationError("ready需要完整事件卡且issues为空。")
        validate_card(result["card"], card_schema, context)
    elif result["card"] is not None or not result["issues"]:
        raise ValidationError("非ready状态必须说明原因且card为null。")
    for issue in result["issues"]:
        nonempty(issue, "issues")
