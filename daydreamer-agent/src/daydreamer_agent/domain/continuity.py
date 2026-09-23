"""Textual continuity contracts; these do not certify generated media continuity."""
from copy import deepcopy
from daydreamer_agent.domain.errors import ValidationError

STATE_FIELDS = ("camera_position", "view_direction", "camera_motion", "layout", "object_states", "lighting")


def state_description(state):
    return "；".join(state[key] for key in STATE_FIELDS)


def resolve_continuity_references(story):
    """Expand an explicit previous-shot reference without rewriting authored states."""
    result = deepcopy(story)
    if not isinstance(result.get("shots", []), list):
        raise ValidationError("shots 必须是分镜数组。")
    previous = None
    for shot in result.get("shots", []):
        if not isinstance(shot, dict):
            raise ValidationError("每个分镜必须是对象。")
        continuity = shot.get("continuity")
        if not isinstance(continuity, dict):
            previous = shot
            continue
        start = continuity.get("start")
        start_reference = deepcopy(start)
        if isinstance(start, dict) and "from_shot" in start:
            if set(start) != {"from_shot"} or previous is None or start["from_shot"] != previous.get("shot_id"):
                raise ValidationError("continuity.start.from_shot 只能单独引用紧邻的前一镜头。")
            previous_continuity = previous.get("continuity")
            end = previous_continuity.get("end") if isinstance(previous_continuity, dict) else None
            if not isinstance(end, dict) or set(end) != set(STATE_FIELDS):
                raise ValidationError("被引用的前镜结束状态缺少六项字段。")
            continuity["start"] = deepcopy(end)
        # Models sometimes repeat structured states in the legacy text fields.
        # Convert only exact duplicates; inconsistent states still fail validation.
        for field, boundary in (("start_state", "start"), ("end_state", "end")):
            authored, resolved = shot.get(field), continuity.get(boundary)
            identical = authored == resolved or (boundary == "start" and authored == start_reference)
            if isinstance(authored, dict) and identical and isinstance(resolved, dict) and set(resolved) == set(STATE_FIELDS):
                if all(isinstance(resolved[key], str) and resolved[key].strip() for key in STATE_FIELDS):
                    shot[field] = state_description(resolved)
        previous = shot
    return result


def validate_continuity(shots):
    previous = None
    for shot in shots:
        continuity = shot.get("continuity")
        if not isinstance(continuity, dict):
            raise ValidationError("连续片段需要 continuity.start 和 continuity.end 状态对象。")
        for boundary in ("start", "end"):
            state = continuity.get(boundary)
            if not isinstance(state, dict) or set(state) != set(STATE_FIELDS):
                raise ValidationError(f"镜头{shot.get('shot_id', '?')}的continuity.{boundary}必须包含 camera_position、view_direction、camera_motion、layout、object_states、lighting 六项。from_shot仅允许放在continuity.start内，每镜continuity.end必须完整。")
            if any(not isinstance(state[key], str) or not state[key].strip() for key in STATE_FIELDS):
                raise ValidationError("连续状态六项均必须是非空文字。")
        if previous is not None and previous != continuity["start"]:
            raise ValidationError("相邻片段必须逐字段复用前段 continuity.end 作为后段 continuity.start，不能改变机位、动作或场景状态。")
        previous = continuity["end"]


def continuity_instruction(mode):
    shared = (
        "全片是一段连续的第一人称经历，不跳时间、不换地点、不切机位，不重新建立场景。"
        "每镜在四项prompt之外增加continuity对象，含start和end；两者都严格包含六个非空文本字段："
        "camera_position（眼睛所在位置及高度）、view_direction（视线方向）、"
        "camera_motion（移动方向、速度及正在进行的动作）、layout（场景与物件相对位置）、"
        "object_states（人物、手、持有物、物件状态及正在发生的变化）、lighting（光源方向、亮度与色彩）。"
        "后一镜start必须逐字复用前一镜end的六个字段，真实末帧将在制作阶段另行传给视频模型。"
        "在自然动作边界拆段，承接实际运动，允许情节所需的转弯、减速、停车或加速，不要求全片匀速直线移动。"
        "object_states同时记录当前目标对象、障碍状态、已完成动作和待继续动作，不能只列外观。"
        "视野之外的变化不要写成眼前可见，除非先描述转头或合理反射。"
        "位置、时长和行动应相容，不随意编造精确距离或速度；不以黑场、叠化或突然停顿掩饰跳变。"
    )
    if mode == "single_take":
        return shared + "本次只能使用文生视频，必须将所有剧情合并为一个3至15秒镜头；无法满足时返回needs_resolution。"
    return shared + (
        "第一段文生视频；后续段以上一段最后使用的画面作首帧进行图生视频，提示词只描述接下来发生的动作，不重演前段。"
        '为避免抄写时改动状态，后续镜头的continuity.start请只输出引用对象，例如{"from_shot":"shot_01"}，'
        "值为紧邻前镜shot_id。程序会将引用展开为前镜end的六字段；第一镜start与所有end仍须完整六字段。"
    )
