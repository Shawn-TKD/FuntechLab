"""One result per story stage, with explicit and recoverable Qwen seeds."""
from pathlib import Path
import secrets

from daydreamer_agent.domain.errors import CreativeRepairExhausted, ValidationError
from daydreamer_agent.storage.files import read_json, write_json
from daydreamer_agent.story.creativity import stage_seed
from daydreamer_agent.story.diversity import ready, request, validate_candidates
from daydreamer_agent.story.generator import STAGES
from daydreamer_agent.story.assembly import generate_assembly


def recover_viewpoint_failure(state):
    """One-time migration of the old lexical gate; never reset the restart budget."""
    if state.get("viewpoint_defaults_version") == 1:
        return False
    state["viewpoint_defaults_version"] = 1
    current = state["attempts"][-1]
    if (current["status"] == "exhausted"
            and current.get("failed_stage", "").startswith("shot-")
            and current.get("reason") == "创作阶段格式修正次数已用完：每镜画面内容需明确第一人称。"):
        current["viewpoint_failure_recovery"] = {"stage": current.pop("failed_stage"), "reason": current.pop("reason")}
        current["status"] = "active"
        return True
    return False


def use_seed_only(path):
    """Freeze already selected stages; never draw from unfinished candidate sets."""
    snapshot_path = path / "input/creativity.json"
    snapshot = read_json(snapshot_path)
    if snapshot.get("mode") == "seed_only":
        return snapshot
    backup = path / "input/creativity-before-seed-only.json"
    if not backup.exists():
        write_json(backup, snapshot)
    reused = {}
    rounds = sorted((path / "story/diversity").glob("round-*"), reverse=True)
    if rounds:
        choices_path = rounds[0] / "choices.json"
        choices = read_json(choices_path) if choices_path.exists() else {}
        for name in ("theme", "script", "visual_style"):
            if name not in choices:
                break
            result = read_json(rounds[0] / f"{name}.json")["output"]
            selected = [item for item in result["candidates"] if item["candidate_id"] == choices[name]["candidate_id"]]
            if len(selected) != 1:
                raise ValidationError("旧阶段已选内容不完整，无法迁移。")
            reused[name] = {"status": "ready", name: selected[0][name], "issues": []}
    write_json(path / "input/reused-story-stages.json", reused)
    snapshot = {"version": 2, "mode": "seed_only", "seed": snapshot["seed"],
                "policy": {"enabled": True, "temperature": snapshot["policy"].get("temperature", 1.0)}}
    write_json(snapshot_path, snapshot)
    return snapshot


def generate_seeded_story(run_path, inputs, skill, provider, progress=print):
    path = Path(run_path)
    snapshot = use_seed_only(path)
    state_path = path / "story/creation-attempts.json"
    if state_path.exists():
        state = read_json(state_path)
        if state.get("assembly_version") != 1:
            current = state["attempts"][-1]
            if current["status"] == "exhausted" and current.get("failed_stage") == "story":
                current["legacy_failure"] = {"stage": current["failed_stage"], "reason": current.get("reason")}
                current["status"] = "active"
                progress("旧整包故事阶段已改为分镜组装，复用本轮已确定的主题、脚本和画风。")
            state["assembly_version"] = 1
            write_json(state_path, state)
    else:
        config = read_json(path / "input/config.json")
        limit = config.get("story", {}).get("max_creation_restarts", 2)
        if type(limit) is not int or not 0 <= limit <= 5:
            raise ValidationError("story.max_creation_restarts必须为0至5的整数。")
        state = {"assembly_version": 1, "max_restarts": limit, "attempts": [{"seed": snapshot["seed"], "status": "active"}]}
        write_json(state_path, state)
    if state.get("viewpoint_defaults_version") != 1:
        recovered = recover_viewpoint_failure(state)
        write_json(state_path, state)
        if recovered:
            progress("已修复旧第一人称关键词校验，复用本轮已保存内容继续分镜，不更换seed或重置重开次数。")
    while True:
        index = len(state["attempts"]) - 1
        current = state["attempts"][-1]
        if current["status"] == "exhausted":
            if index >= state["max_restarts"]:
                raise ValidationError(f"创作自动重开{state['max_restarts']}次后仍未通过，已保存全部记录；未提交视频。")
            used = {attempt["seed"] for attempt in state["attempts"]}
            seed = secrets.randbelow(2**31)
            while seed in used:
                seed = (seed + 1) % 2**31
            current = {"seed": seed, "status": "active"}
            state["attempts"].append(current)
            write_json(state_path, state)  # Commit the new seed before any paid request.
            index += 1
            progress(f"格式修正次数已用完，自动重开创作 {index}/{state['max_restarts']}，使用新seed从主题开始。")
        try:
            output = generate_attempt(path, inputs, skill, provider, snapshot, current["seed"], index, progress)
        except CreativeRepairExhausted as exc:
            current.update(status="exhausted", failed_stage=exc.stage, reason=str(exc))
            write_json(state_path, state)
            continue
        current["status"] = "ready" if output["status"] == "ready" else output["status"]
        write_json(state_path, state)
        return output


def generate_attempt(path, inputs, skill, provider, snapshot, seed, index, progress):
    stage_path = path / ("story/seeded" if index == 0 else f"story/seeded-restart-{index:02}")
    progress(f"本次创作随机种子：{seed}（单方案，各阶段传入派生seed）")
    reused_path = path / "input/reused-story-stages.json"
    reused = read_json(reused_path) if index == 0 and reused_path.exists() else {}
    prior = {}
    for name, instruction in STAGES[:3]:
        def validate(output):
            if not ready(output):
                return output
            if name not in output:
                raise ValidationError("阶段缺少字段：" + name)
            validate_candidates({"status": "ready", "candidates": [{"candidate_id": "single", name: output[name]}], "issues": []}, name, 1, inputs)
            return output
        if name in reused:
            output = validate(reused[name])
            progress("复用已确定内容：" + name)
        else:
            output = request(stage_path, name,
                             "\n\n".join(skill.values()) + "\n当前阶段：" + instruction,
                             {"input": inputs, "upstream": prior, "operation": "single_stage", "stage": name},
                             provider, stage_seed(seed, 0, name), snapshot["policy"]["temperature"], validate, progress)
        if output["status"] != "ready":
            return output
        prior[name] = output
    return generate_assembly(stage_path, inputs, prior, provider, seed, snapshot["policy"]["temperature"], progress)
