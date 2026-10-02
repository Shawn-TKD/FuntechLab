"""Deterministic duration allocation; never change shot boundaries or actions."""
from daydreamer_agent.providers.video_models import minimum_duration
from copy import deepcopy
import math

from daydreamer_agent.domain.errors import FieldValidationError
from daydreamer_agent.story.pacing import validate_policy, validate_timing_reason


def validate_timing_constraints(constraints):
    validate_policy(constraints)
    minimum = minimum_duration(constraints)
    target = constraints.get("duration_seconds")
    cap = constraints.get("max_duration_seconds")
    if cap is not None and (type(cap) is not int or cap < minimum):
        raise FieldValidationError("production_constraints.max_duration_seconds", f"时长上限必须为至少{minimum}秒的整数。")
    if target is None:
        return None
    if type(target) not in (int, float) or not math.isfinite(target) or target < minimum or int(target) != target:
        raise FieldValidationError("production_constraints.duration_seconds", f"总时长必须是至少{minimum}秒的整数。")
    target = int(target)
    if cap is not None and target > cap:
        raise FieldValidationError("production_constraints.duration_seconds", f"指定总时长不能超过{cap}秒。")
    limit = constraints.get("shot_limit")
    if limit is not None and (type(limit) is not int or limit < 1):
        raise FieldValidationError("production_constraints.shot_limit", "镜头数上限必须是正整数。")
    if constraints.get("continuity_mode") == "single_take":
        limit = 1
    if limit is not None and target > limit * 15:
        raise FieldValidationError("production_constraints", f"总长{target}秒超过{limit}镜可容纳的{limit * 15}秒，请调整时长或镜头数限制。")
    return target


def fit_plan_timing(plan, constraints):
    minimum = minimum_duration(constraints)
    result = deepcopy(plan)
    shots = result["shots"]
    target = validate_timing_constraints(constraints)
    cap = constraints.get("max_duration_seconds")
    if target is None and cap is None:
        return result
    count = len(shots)
    if not count:
        raise FieldValidationError("shots", "分镜不能为空。")
    proposed = [shot["duration_seconds"] for shot in shots]
    if constraints.get("pacing_prompt_version"):
        limit = constraints.get("shot_limit")
        if limit is not None and (type(limit) is not int or limit < 1 or count > limit):
            raise FieldValidationError("shots", "镜头数须遵守有效的shot_limit。")
        if constraints.get("continuity_mode") == "single_take" and count != 1:
            raise FieldValidationError("shots", "single_take只能有一个镜头。")
        for index, value in enumerate(proposed):
            if type(value) is not int or not minimum <= value <= 15:
                raise FieldValidationError(f"shots[{index}].duration_seconds", f"新时长策略须填写{minimum}至15秒的具体整数，不能为null。")
        if target is None:
            target = sum(proposed)
            if target > cap:
                raise FieldValidationError("shots", f"自动总长{target}秒超过{cap}秒上限；请重新按必要动作规划，不能靠机械压缩或截断。")
        validate_timing_reason(result, constraints, target)
    for index, value in enumerate(proposed):
        if value is not None and (type(value) not in (int, float) or not math.isfinite(value) or value <= 0):
            raise FieldValidationError(f"shots[{index}].duration_seconds", "建议时长必须为有限正数或null，不能是字符串、布尔值或负数。")
    if target is None:
        # Respect suggested pacing without stretching every film to the cap.
        if constraints.get("continuity_mode") == "single_take":
            cap = min(cap, 15)
        if minimum * count > cap:
            raise FieldValidationError("shots", f"总长上限{cap}秒最多容纳{cap // minimum}镜；请按动作边界合并相邻beat，保留全部剧情覆盖。")
        suggested = sum(min(15, max(minimum, 6 if value is None else value)) for value in proposed)
        target = min(cap, max(minimum * count, int(suggested + 0.5)))
    if not minimum * count <= target <= 15 * count:
        low, high = (target + 14) // 15, target // minimum
        if constraints.get("continuity_mode") == "single_take":
            high = min(high, 1)
        if constraints.get("shot_limit") is not None:
            high = min(high, constraints["shot_limit"])
        raise FieldValidationError("shots", f"当前{count}镜无法满足总长{target}秒和每镜{minimum}至15秒。"
                                   f"镜头数必须在{low}至{high}之间；按相邻动作边界重新规划，保留全部beat覆盖。")
    if all(type(value) is int and minimum <= value <= 15 for value in proposed) and sum(proposed) == target:
        return result
    # Missing suggestions receive the mean duration. Ratios preserve the model's
    # pacing as far as the integer bounds allow; ties follow shot order.
    weights = [target / count if value is None else value for value in proposed]
    scale = max(weights)
    weights = [value / scale for value in weights]
    desired = [target * value / sum(weights) for value in weights]
    durations = [minimum] * count
    for _ in range(target - minimum * count):
        index = min((i for i in range(count) if durations[i] < 15),
                    key=lambda i: (2 * (durations[i] - desired[i]) + 1, i))
        durations[index] += 1
    for shot, duration in zip(shots, durations):
        shot["duration_seconds"] = duration
    return result


def recover_timing_failure(state):
    if state.get("plan_timing_version") == 1:
        return False
    state["plan_timing_version"] = 1
    current = state["attempts"][-1]
    reasons = {"创作阶段格式修正次数已用完：每镜须为3至15秒整数。",
               "创作阶段格式修正次数已用完：分镜规划总时长与制作约束不符。"}
    if current["status"] == "exhausted" and current.get("failed_stage") == "plan" and current.get("reason") in reasons:
        current["timing_failure_recovery"] = {"stage": current.pop("failed_stage"), "reason": current.pop("reason")}
        current["status"] = "active"
        return True
    return False
