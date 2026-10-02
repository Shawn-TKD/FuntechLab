import json

from daydreamer_agent.domain.errors import ValidationError
from daydreamer_agent.domain.continuity import state_description
from daydreamer_agent.storage.files import digest
from daydreamer_agent.story.validation import PROMPT_KEYS
from daydreamer_agent.story.sound import sound_execution
from daydreamer_agent.story.pacing import PACING_EXECUTION

from daydreamer_agent.providers.video_models import H3, capabilities

TEMPLATE_VERSION = "2.3"
RATIOS = {"16:9", "9:16", "1:1", "4:3", "3:4", "21:9"}


def compile_shot(story, shot, constraints, model, *, sound_version=1):
    style = story["visual_style"]
    labels = {"medium": "画面媒介", "form_and_space": "造型与空间", "palette": "色彩", "materials": "材质", "lighting": "光线", "motion_character": "运动表现"}
    sections = ["【视点约束】\n全程第一人称，摄像机代表经历者的眼睛；视点运动有身体行动依据。仅呈现当前视野可见的事件，身后变化可用声音传达，不使用全知视角。"]
    index = next(i for i, item in enumerate(story["shots"]) if item["shot_id"] == shot["shot_id"])
    current_beats = [beat for beat in story["script"] if beat["beat_id"] in shot["beat_ids"]]
    narrative = {
        "持续行动目标": story["theme"].get("action_goal") or story["theme"]["statement"],
        "此前已发生_仅作承接不要重演": story["shots"][index - 1]["end_state"] if index else "从本段起始处境直接进入行动。",
        "本段关联剧情_按本镜起止范围执行": [
            {key: beat[key] for key in ("first_person_action", "visible_change", "end_state")}
            for beat in current_beats
        ],
        "本镜动作范围": {"开始": shot["start_state"], "结束": shot["end_state"]},
        "下一段衔接": shot["transition_to_next"],
    }
    sections.append("【连续行动剧情】\n" + json.dumps(narrative, ensure_ascii=False))
    sections.append("【剧情执行约束】\n围绕同一目标执行本镜动作及其后果，不重演已经完成的动作，不提前演完后续事件；同一剧情段落跨镜时只执行本镜范围。无反转，不突然换目标，不插入无关奇观。末镜在自然动作节点结束，整场追逐或交战可以继续，不擅自增加胜利、揭晓或反转结局。")
    sections.append("【世界与空间约束】\n仅使用已声明的幻想能力，其余动作遵守支撑、接触、碰撞和可通行空间的常识。交通工具在已建立的承载方式下移动，不把更名或比喻当成功能转换；不凭空替换道路、堆叠悬空物件或增加能力。")
    sections.append("【非写实表达】\n全画面统一非写实，无写实、微写实或半写实元素，双手与环境风格一致。使用明确轮廓、体积、遮挡和支撑关系表达空间；非写实不等于抽象几何堆砌，不因画风改变物体运行规则。")
    sections.append("【本片视觉风格】\n" + "\n".join(f"{label}：{style[key]}" for key, label in labels.items()))
    sections.append("【稳定特征】\n" + json.dumps(style["stable_constraints"], ensure_ascii=False))
    if style["transitions"]:
        sections.append("【情节引发的风格过渡】\n" + json.dumps(style["transitions"], ensure_ascii=False))
    sections.append("【统一主题与幻想规则】\n" + story["theme"]["statement"] + "\n" + story["theme"]["core_rule"] + "\n主题边界：" + story["theme"]["boundary"])
    continuous = constraints.get("continuity_mode") in {"single_take", "frame_chain"}
    if not continuous or shot["start_state"] != state_description(shot["continuity"]["start"]):
        sections.append("【起始状态】\n" + shot["start_state"])
    if continuous:
        sections.append("【连续拍摄约束】\n从开始到结束保持同一第一人称连续视点，不剪切、不重置机位、不突然跳变时间、空间、光照或物体状态。动作方向与速度自然延续。")
        sections.append("【连续状态】\n" + json.dumps(shot["continuity"], ensure_ascii=False))
        if constraints.get("continuity_mode") == "frame_chain" and story["shots"][0]["shot_id"] != shot["shot_id"]:
            sections.append("【首帧续接】\n提供的首帧是上一段实际结束画面，以它的空间、物件、色彩与光照作为本段起点，延续正在进行的动作。只推进后续事件，不重演上一段，不重新建立场景。")
    sections.extend(f"【{name}】\n{shot['prompt'][name]}" for name in PROMPT_KEYS)
    if constraints.get("pacing_prompt_version"):
        sections.append("【本镜节奏】\n" + PACING_EXECUTION)
    if not continuous or shot["end_state"] != state_description(shot["continuity"]["end"]):
        sections.append("【结束状态】\n" + shot["end_state"])
    if capabilities(model)["native_audio"]:
        sound = shot["audio"]
        sections.extend(("【音效】\n" + ("\n".join(sound["sfx"]) or "不添加未指定的环境声或动作音效。"),
                         "【背景音乐】\n" + sound["music"],
                         "【声音衔接】\n" + sound["continuity"],
                         "【声音限制】\n" + sound_execution(sound_version)))
    prompt = "\n\n".join(sections)
    if len(prompt) > capabilities(model)["prompt_limit"]:
        raise ValidationError(f"镜头 {shot['shot_id']} 提示词过长，请精简分镜；不会自动截断。")
    parameters = {"duration": shot["duration_seconds"], "ratio": constraints.get("aspect_ratio"), "resolution": constraints.get("resolution"), "audio": False, "prompt_extend": False}
    if model == H3:
        parameters.pop("audio")
        parameters.pop("prompt_extend")
    request = {"model": model, "input": {"prompt": prompt}, "parameters": parameters}
    version = TEMPLATE_VERSION if sound_version == 2 or constraints.get("pacing_prompt_version") else "2.2"
    return {"template_version": version, "story_digest": digest(story), "request": request}


def validate_video_request(request):
    spec = capabilities(request.get("model"))
    prompt = request.get("input", {}).get("prompt")
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > spec["prompt_limit"]:
        raise ValidationError(f"视频提示词必须非空且不超过{spec['prompt_limit']}字符。")
    p = request["parameters"]
    if type(p.get("duration")) is not int or not spec["minimum"] <= p["duration"] <= 15:
        raise ValidationError(f"每镜视频时长必须是 {spec['minimum']}–15 秒的整数；请修改故事并创建新任务。")
    if p.get("ratio") not in RATIOS:
        raise ValidationError("生成前需要明确有效画幅，例如 16:9 或 9:16。")
    if p.get("resolution") not in spec["resolutions"]:
        raise ValidationError("当前视频模型支持的成片分辨率：" + "、".join(sorted(spec["resolutions"])))
