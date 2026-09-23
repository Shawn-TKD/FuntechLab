from pathlib import Path

from daydreamer_agent.domain.errors import ValidationError
from daydreamer_agent.domain.continuity import continuity_instruction, resolve_continuity_references
from daydreamer_agent.storage.files import digest, read_json, write_json
from daydreamer_agent.story.validation import validate_story

STAGES = (
    ("theme", "只生成一个有明确行动目标的幻想情境并输出 theme；先不选画风。"
     "statement说明我正在对谁或什么做什么，action_goal用一句话固定全片持续目标。"
     "core_rule说明必要的幻想能力、触发条件、作用边界，其余空间、支撑、碰撞与行动沿用常识。"
     "development是一段连续行动，可为追逐、交战、穿越或其他适合生活动作的经历；不是奇观列表。返回 {status, theme, issues}。"),
    ("script", "依据已确定 theme 输出 script；先写同一目标下连续发生的动作，再考虑分镜，不提前选画风。"
     "每个beat的first_person_action是具体应对，visible_change是该行动或既定规则产生的可见后果，"
     "end_state同时交代位置、目标/障碍状态及正在延续的行动，让下一beat接着推进。"
     "不设置反转、不套起承转合，不用重复向前走加环境变形充当剧情；可从行动中开始，在自然动作节点结束而不结束整场事件。"
     "远处或身后的事件只按第一人称实际可见、可听范围描述。返回 {status, script, issues}。"),
    ("visual_style", "依据完整 script 选择明确非写实的 visual_style，不改写情节，不继承旧画风。"
     "禁止写实、微写实与半写实；非写实不等于抽象或空间扁平化，保留易读的体积、道路、支撑与遮挡关系，"
     "让目标、动作及作用结果清楚可辨。返回 {status, visual_style, issues}。"),
    ("story", "保留已确定的 theme、script、visual_style，输出完整故事包，补齐 audio_plan、shots 和其他顶层字段。"
     "按动作的自然衔接划分镜头和时长，不为凑镜数平均分配，不在每段重新建立场景或重演上段动作。"
     "每镜只推进所引用beat在本段起止状态之间的动作；同一beat跨镜时须明确分段进展。"
     "起止状态同时写清行动进展与目标、障碍状态，四项prompt与状态描述必须一致。"
     "禁止反转、突兀换目标或无因果的物件变形；结尾可继续任务，但须完成本段动作，不强行收束整场故事。"
     "硬性制作约束：每镜时长为3到15秒整数，合计等于指定总时长；短总时长应合并beat而非挤入过多动作。"
     "例如6秒只能有1或2镜，不能出现2秒镜头。无需编写精确米数或速度，除非输入明确要求；写出一致的相对位置与移动过程即可。"
     "直接交付，不执行生成后的内容审阅、评分或复核，checks固定为空数组，不编造pass结论。"),
)


def load_skill(directory):
    directory = Path(directory)
    files = ["SKILL.md", "references/input-output-schema.md", "references/style-selection.md"]
    return {name: (directory / name).read_text(encoding="utf-8") for name in files}


def generate_story(run_path, inputs, skill, provider, progress=lambda _: None):
    run_path = Path(run_path)
    prior = {}
    for name, instruction in STAGES:
        mode = inputs.get("production_constraints", {}).get("continuity_mode")
        if mode in {"single_take", "frame_chain"}:
            instruction += "\n" + continuity_instruction(mode)
        fingerprint = digest({"input": inputs, "skill": skill, "prior": prior, "instruction": instruction})
        checkpoint = run_path / "story" / f"{name}.json"
        if checkpoint.exists():
            saved = read_json(checkpoint)
            if saved["basis"] != fingerprint:
                raise ValidationError("创作输入或规则已变更，请创建新任务以保留旧版本。")
            output = saved["output"]
        else:
            progress("正在生成：" + name)
            instructions = "\n\n".join(skill.values()) + "\n\n当前阶段：" + instruction
            context = {"input": inputs, "upstream": prior}
            # Retry malformed creative output once, retaining both responses for diagnosis.
            for attempt in range(2):
                output, usage = provider.generate(instructions, context)
                response_path = run_path / "story" / "responses" / f"{name}-{attempt + 1}.json"
                write_json(response_path, {"output": output, "usage": usage})
                try:
                    if mode == "frame_chain" and name == "story":
                        resolved = resolve_continuity_references(output)
                        if resolved != output:
                            write_json(response_path, {"output": output, "resolved_output": resolved, "usage": usage})
                        output = resolved
                    if output.get("status") not in {"ready", "wait_for_material", "needs_resolution"}:
                        raise ValidationError("每阶段都必须返回有效 status。")
                    if output["status"] != "ready":
                        if not isinstance(output.get("issues"), list) or not output["issues"]:
                            raise ValidationError("非 ready 状态必须提供 issues。")
                    elif name == "story":
                        validate_story(output, inputs)
                    elif name not in output or not output[name]:
                        raise ValidationError("阶段缺少有效内容：" + name)
                    break
                except ValidationError as exc:
                    if attempt == 1:
                        raise
                    context = {"input": inputs, "upstream": prior, "invalid_output": output, "repair": str(exc)}
            write_json(checkpoint, {"basis": fingerprint, "output": output})
        if output["status"] != "ready":
            return output
        prior[name] = output
    return prior["story"]
