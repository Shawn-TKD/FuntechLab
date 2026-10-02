"""Versioned creative pacing policy; no additional model or review stage."""
from daydreamer_agent.domain.errors import FieldValidationError


PACING_DIRECTION = """从主题和脚本起按本次时长预算组织一段连续行动，尽快进入有意义的动作，不先安排无意义的出发、看景或背景介绍。
每个beat推进位置、目标关系、障碍、决定或结果；合并重复观察、重复接近及没有新后果的移动，不为凑时长添加新事件。
保留理解动作所需的反应、转向、因果与空间过渡；有叙事作用的安静或停顿可以保留。不要靠堆叠事件、频繁切镜、机械倍速、无意义慢动作或截断动作制造紧凑。
镜头按自然动作边界安排，不平均分配，不把单镜15秒上限当作每镜目标；在自然动作节点结束，可以继续整场事件而不强行收尾。"""

PACING_EXECUTION = "在本镜规定时长内自然、紧凑地完成指定动作及必要反应，不重演前镜、不无意义停留或慢放、不额外巡游展示环境，不提前执行后镜。保留动作因果与空间过渡，不机械加速或截断关键动作。"

TIMING_REASON_DIRECTION = """额外输出内部timing_reason对象，仅含reason、extension_reason、extension_beat_ids。reason解释动作所需的总时长。
自动总时长超过preferred_max_duration_seconds时，extension_reason必须具体说明压入优先区间会遗漏的必要动作或因果，extension_beat_ids引用对应已有beat；其他情况分别为空字符串和空数组。
“更有电影感”“音乐还没结束”“画面好看”或凑镜头数不是延长依据。直接在本次规划中作出决定，不输出内容评分。"""


def validate_policy(constraints):
    version = constraints.get("pacing_prompt_version", 0)
    if type(version) is not int or version not in (0, 1):
        raise FieldValidationError("pacing_prompt_version", "必须为0或1。")
    if not version:
        return
    low = constraints.get("preferred_min_duration_seconds")
    high = constraints.get("preferred_max_duration_seconds")
    cap = constraints.get("max_duration_seconds")
    if any(type(v) is not int for v in (low, high, cap)) or not 1 <= low <= high <= cap:
        raise FieldValidationError("production_constraints", "优先时长与上限必须为整数，满足1 ≤ 优先最短 ≤ 优先最长 ≤ 总长上限。")


def configured_policy(config):
    version = config.get("story", {}).get("pacing_prompt_version", 0)
    result = {"pacing_prompt_version": version}
    if version:
        defaults = config.get("workflow", {})
        for name in ("preferred_min_duration_seconds", "preferred_max_duration_seconds", "max_duration_seconds"):
            result[name] = defaults.get(name)
    validate_policy(result)
    return result


def freeze_policy(constraints, config):
    """Call only when creating a task; never inject new defaults on resume."""
    policy = configured_policy(config)
    if policy["pacing_prompt_version"]:
        cap = constraints.get("max_duration_seconds")
        if cap is not None:
            if type(cap) is not int or cap < policy["preferred_max_duration_seconds"]:
                raise FieldValidationError("max_duration_seconds", "输入上限必须为整数且不小于优先时长上界；更短视频可显式指定duration_seconds。")
            policy["max_duration_seconds"] = min(cap, policy["max_duration_seconds"])
        constraints.update(policy)
    else:
        # Do not allow an event card to upgrade a frozen legacy policy.
        for name in ("pacing_prompt_version", "preferred_min_duration_seconds", "preferred_max_duration_seconds"):
            constraints.pop(name, None)


def pacing_instruction(constraints):
    validate_policy(constraints)
    if not constraints.get("pacing_prompt_version"):
        return ""
    target = constraints.get("duration_seconds")
    low, high = (constraints[k] for k in ("preferred_min_duration_seconds", "preferred_max_duration_seconds"))
    if target is not None:
        budget = f"本次明确指定总时长{target}秒，优先遵守此长度，不用默认优先区间覆盖。"
    else:
        budget = (f"本次自动总时长优先{low}–{high}秒，上限{constraints['max_duration_seconds']}秒。"
                  f"不足{low}秒已表达清楚则保持更短，不凑下限；超过{high}秒仅因必要动作和因果表达需要，不默认用满上限。")
    if constraints.get("continuity_mode") == "single_take":
        budget += "single_take仅允许一镜且最多15秒，优先区间不能突破此限制。"
    return budget + "\n" + PACING_DIRECTION


def validate_timing_reason(plan, constraints, total):
    reason = plan.get("timing_reason")
    keys = {"reason", "extension_reason", "extension_beat_ids"}
    if not isinstance(reason, dict) or set(reason) != keys:
        raise FieldValidationError("timing_reason", "必须仅包含reason、extension_reason、extension_beat_ids。")
    if not isinstance(reason["reason"], str) or not reason["reason"].strip():
        raise FieldValidationError("timing_reason.reason", "必须说明动作所需时长。")
    extended = reason["extension_beat_ids"]
    allowed = set()
    for shot in plan["shots"]:
        beats = shot.get("beat_ids")
        if not isinstance(beats, list) or any(not isinstance(beat, str) for beat in beats):
            raise FieldValidationError("shots.beat_ids", "必须为已有beat的字符串引用数组。")
        allowed.update(beats)
    if (not isinstance(extended, list) or any(not isinstance(v, str) or v not in allowed for v in extended)
            or len(set(extended)) != len(extended)):
        raise FieldValidationError("timing_reason.extension_beat_ids", "必须为不重复的已有beat引用数组。")
    detail = reason["extension_reason"]
    if not isinstance(detail, str):
        raise FieldValidationError("timing_reason.extension_reason", "必须为字符串。")
    if constraints.get("duration_seconds") is None and total > constraints["preferred_max_duration_seconds"]:
        if not detail.strip() or not extended:
            raise FieldValidationError("timing_reason", "自动总长超过优先区间，需提供必要动作的extension_reason及extension_beat_ids；否则重新规划到优先区间。")
    elif detail or extended:
        raise FieldValidationError("timing_reason", "未超出自动优先区间时，extension_reason为空字符串，extension_beat_ids为空数组。")


def timing_record(plan, constraints, proposed=None):
    allocated = [shot["duration_seconds"] for shot in plan["shots"]]
    return {"version": 2, "pacing_prompt_version": constraints["pacing_prompt_version"],
            "preferred_seconds": [constraints["preferred_min_duration_seconds"], constraints["preferred_max_duration_seconds"]],
            "target_seconds": constraints.get("duration_seconds"), "max_seconds": constraints["max_duration_seconds"],
            "proposed_seconds": allocated if proposed is None else proposed, "allocated_seconds": allocated,
            "actual_seconds": sum(allocated), "timing_reason": plan["timing_reason"]}
