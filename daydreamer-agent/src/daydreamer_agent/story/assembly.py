"""Generate only missing fields, then assemble immutable creative stages locally."""
from copy import deepcopy

from daydreamer_agent.domain.continuity import STATE_FIELDS, state_description
from daydreamer_agent.domain.errors import FieldValidationError
from daydreamer_agent.storage.files import digest, write_json
from daydreamer_agent.story.creativity import stage_seed
from daydreamer_agent.story.diversity import ready, request
from daydreamer_agent.story.validation import collect_memory_ids, refs, require, text_fields, validate_story


def obj(properties):
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


TEXT = {"type": "string"}
TEXTS = {"type": "array", "items": TEXT}
STATUS = {"type": "string", "enum": ["ready", "wait_for_material", "needs_resolution"]}
STATE = obj({key: TEXT for key in STATE_FIELDS})
PROMPT = obj({key: TEXT for key in ("景别", "构图", "运镜手法", "画面内容")})
SPEECH_POLICY = "仅背景音乐与音效，无旁白、对白、演唱、人声采样或原始人声音轨。"
VIEWPOINT_DIRECTIVE = "第一人称视角，摄像机代表经历者的眼睛。"


def normalize_shot_prompt(prompt):
    """Apply a fixed production directive, without changing the saved model response."""
    keys = PROMPT["properties"]
    if not isinstance(prompt, dict):
        raise FieldValidationError("shot.prompt", "必须为包含景别、构图、运镜手法、画面内容的对象。")
    for key in keys:
        if not isinstance(prompt.get(key), str) or not prompt[key].strip():
            raise FieldValidationError("shot.prompt." + key, "必须填写非空字符串，描述当前镜头的具体内容。")
    if set(prompt) != set(keys):
        raise FieldValidationError("shot.prompt", "仅允许景别、构图、运镜手法、画面内容四个字段。")
    result = deepcopy(prompt)
    if "第一人称" not in result["画面内容"]:
        result["画面内容"] = VIEWPOINT_DIRECTIVE + result["画面内容"]
    return result

PLAN_SCHEMA = obj({
    "status": STATUS, "issues": TEXTS,
    "shots": {"type": "array", "items": obj({"beat_ids": TEXTS,
        "duration_seconds": {"type": ["integer", "null"]}, "action_range": TEXT})},
    "audio_plan": obj({"music_direction": TEXT, "sound_motifs": TEXTS}),
    "assumptions": TEXTS, "used_memory_ids": TEXTS,
    "world_delta_draft": {"type": "array", "items": obj({"change_type": TEXT,
        "target_id": {"type": ["string", "null"]}, "description": TEXT, "basis_beat_ids": TEXTS})},
})

PLAN_PROMPT = """你负责对已经确定的第一人称幻想脚本安排分镜，只输出新增的分镜规划与声音文字方案。
locked中的theme、script、visual_style已确定，不得改写，也不得在输出中重述这些字段。
先按自然动作边界划分镜头，每镜仅输出beat_ids、duration_seconds、action_range；action_range简述本镜从哪一步推进到哪一步，不写完整镜头提示词。
允许一段beat跨镜，但必须按顺序明确不同进展；覆盖全部beat，不倒退、不重演。每镜3至15秒整数，合计必须等于production_constraints.duration_seconds；single_take只能一镜。尊重shot_limit。未指定时长可给null，并在assumptions中说明。
沿用同一持续目标、幻想规则与非写实画风，不增加反转、不临时换目标，不用无因果的变形替代行动，不强行结束整场事件。
补充audio_plan（music_direction、sound_motifs）、assumptions、used_memory_ids和world_delta_draft。记忆ID只能来自已提供的记忆；无需新增世界记忆时world_delta_draft为空。used_memory_ids只列实际采用项。
输出仅含status、issues、shots、audio_plan、assumptions、used_memory_ids、world_delta_draft。不要返回完整故事包、checks或自评；无问题issues为空。需要补充材料时用wait_for_material或needs_resolution说明原因。
只给出简洁可执行的计划，不提前输出prompt和六项连续状态。声音仅背景音乐和音效，无任何人声。
"""

SHOT_PROMPT = """你负责一个且仅一个镜头的具体内容。根据locked主题和画风、action_outline全片行动顺序、current_beats与shot_plan创作本镜，不能改写已确定的主题、脚本或画风。
输出仅含status、issues、shot。shot中只填本次schema指定的新增字段；不要返回主题、脚本、画风、编号、来源引用、时长或其他镜头。
prompt严格包含景别、构图、运镜手法、画面内容。第一人称眼睛视点，全画面统一非写实，无写实或微写实元素；每镜明确具体媒介表现，不能写同上。保留支撑、遮挡、路径与作用因果；不切第三人称机位、不跳时间地点，不重演前镜，不提前执行后镜动作。
本镜行动范围由shot_plan.action_range确定；世界能力不能超出既定边界。目标、障碍及动作状态须随剧情推进，结尾可以继续任务而不强行收束。
end填写六个非空文字字段camera_position、view_direction、camera_motion、layout、object_states、lighting；包含人物行动与物件进展，不只描写静态背景。只在第一镜输出start同样六项；后续镜头previous_end就是不可更改的本镜起点，程序将直接复制，不要重新输出start或from_shot。
audio只含music、sfx（字符串数组）、continuity，全片只有背景音乐与音效，无人声。transition_to_next简述自然动作承接；最后一镜用null，不强制黑场或音乐淡出。
不做审阅、评分或核查，只生成当前镜头字段；文本简洁，不重复全片设定。无法遵守素材约束时返回非ready状态与issues，shot为null。
"""


def exact(value, keys, label):
    require(isinstance(value, dict) and set(value) == set(keys), label + "字段必须严格匹配，不能重述已确定内容。")


def validate_plan(output, locked, inputs):
    if not ready(output):
        return output
    exact(output, PLAN_SCHEMA["properties"], "分镜规划")
    shots = output["shots"]
    require(isinstance(shots, list) and bool(shots), "分镜规划不能为空。")
    constraints = inputs["production_constraints"]
    if constraints.get("continuity_mode") == "single_take":
        require(len(shots) == 1, "single_take仅允许一镜。")
    if constraints.get("shot_limit") is not None:
        require(len(shots) <= constraints["shot_limit"], "镜头数超过shot_limit。")
    beats = {beat["beat_id"]: i for i, beat in enumerate(locked["script"])}
    covered, last, total, known = set(), -1, 0, True
    for shot in shots:
        exact(shot, ("beat_ids", "duration_seconds", "action_range"), "镜头规划")
        text_fields(shot, ("action_range",), "镜头规划")
        refs(shot["beat_ids"], beats, "beat_ids")
        positions = [beats[beat] for beat in shot["beat_ids"]]
        require(positions == sorted(set(positions)) and positions[0] >= last, "分镜须按脚本顺序推进，不可重复倒退。")
        last = positions[-1]
        covered.update(shot["beat_ids"])
        duration = shot["duration_seconds"]
        if duration is None:
            known = False
        else:
            require(type(duration) is int and 3 <= duration <= 15, "每镜须为3至15秒整数。")
            total += duration
    require(covered == set(beats), "分镜必须覆盖所有脚本段。")
    target = constraints.get("duration_seconds")
    if target is not None:
        require(known and total == target, "分镜规划总时长与制作约束不符。")
    exact(output["audio_plan"], ("music_direction", "sound_motifs"), "声音规划")
    text_fields(output["audio_plan"], ("music_direction",), "声音规划")
    for values in (output["assumptions"], output["audio_plan"]["sound_motifs"]):
        require(isinstance(values, list) and all(isinstance(v, str) for v in values), "声音线索及假设须为文字数组。")
    refs(output["used_memory_ids"], collect_memory_ids(inputs.get("story_memory") or {}), "used_memory_ids", nonempty=False)
    require(isinstance(output["world_delta_draft"], list), "记忆增量须为数组。")
    for delta in output["world_delta_draft"]:
        exact(delta, ("change_type", "target_id", "description", "basis_beat_ids"), "记忆增量")
        text_fields(delta, ("change_type", "description"), "记忆增量")
        require(delta["target_id"] is None or isinstance(delta["target_id"], str), "记忆target_id须为文字或null。")
        refs(delta["basis_beat_ids"], beats, "basis_beat_ids")
    return output


def assemble(locked, plan, shots, inputs):
    memory = inputs.get("story_memory") or {}
    event_ids = list(dict.fromkeys(event for beat in locked["script"] for event in beat["source_event_ids"]))
    return {"schema_version": "1.0", "status": "ready", **deepcopy(locked),
            "source_event_ids": event_ids,
            "memory_basis": {"world_id": memory.get("world_id"), "version": memory.get("version"), "used_ids": deepcopy(plan["used_memory_ids"])},
            "assumptions": deepcopy(plan["assumptions"]),
            "audio_plan": {**deepcopy(plan["audio_plan"]), "speech_policy": SPEECH_POLICY},
            "shots": deepcopy(shots), "checks": [], "issues": [],
            "world_delta_draft": [{**deepcopy(delta), "base_memory_version": memory.get("version")} for delta in plan["world_delta_draft"]]}


def build_shot(fragment, item, index, count, previous_end, locked, inputs):
    keys = ("prompt", "audio", "end", "transition_to_next") + (() if previous_end is not None else ("start",))
    exact(fragment, keys, "单镜头")
    start = deepcopy(previous_end if previous_end is not None else fragment["start"])
    end = fragment["end"]
    for state in (start, end):
        exact(state, STATE_FIELDS, "连续状态")
        text_fields(state, STATE_FIELDS, "连续状态")
    events = list(dict.fromkeys(e for beat in locked["script"] if beat["beat_id"] in item["beat_ids"] for e in beat["source_event_ids"]))
    sources = []
    for card in inputs["event_cards"]:
        if card["event_id"] in events:
            for ref in card["source_refs"]:
                if ref not in sources:
                    sources.append(deepcopy(ref))
    require((fragment["transition_to_next"] is None) == (index == count - 1), "仅最后一镜的transition_to_next必须为null。")
    exact(fragment["audio"], ("music", "sfx", "continuity"), "镜头声音")
    text_fields(fragment["audio"], ("music", "continuity"), "镜头声音")
    require(isinstance(fragment["audio"]["sfx"], list) and all(isinstance(x, str) for x in fragment["audio"]["sfx"]), "sfx须为文字数组。")
    return {"shot_id": f"shot-{index + 1:03}", "beat_ids": deepcopy(item["beat_ids"]),
            "source_event_ids": events, "source_refs": sources,
            "duration_seconds": item["duration_seconds"],
            "duration_basis": "unspecified" if item["duration_seconds"] is None else ("confirmed" if inputs["production_constraints"].get("duration_seconds") is not None else "proposal"),
            "start_state": state_description(start), "end_state": state_description(end),
            "continuity": {"start": start, "end": deepcopy(end)},
            "action_range": item["action_range"], "prompt": normalize_shot_prompt(fragment["prompt"]),
            "audio": deepcopy(fragment["audio"]), "transition_to_next": fragment["transition_to_next"]}


def generate_assembly(stage_path, inputs, prior, provider, seed, temperature, progress):
    root = stage_path / "assembly-v1"
    locked = {key: deepcopy(prior[key][key]) for key in ("theme", "script", "visual_style")}
    # Creative outputs are provided once as authoritative context, never requested as output.
    plan_context = {"operation": "story_assembly", "stage": "story_plan", "locked": locked,
                    "production_constraints": inputs["production_constraints"],
                    "story_memory": inputs.get("story_memory"), "current_preferences": inputs.get("current_preferences"),
                    "events": [{key: card[key] for key in ("event_id", "summary", "unknowns") if key in card} for card in inputs["event_cards"]]}
    plan = request(root, "plan", PLAN_PROMPT, plan_context, provider, stage_seed(seed, 0, "story_plan"), temperature,
                   lambda output: validate_plan(output, locked, inputs), progress,
                   request_options={"schema": PLAN_SCHEMA, "max_tokens": 8192})
    if plan["status"] != "ready":
        return plan
    shots, prompt_defaults = [], []
    for index, item in enumerate(plan["shots"]):
        previous_end = shots[-1]["continuity"]["end"] if shots else None
        fragment_fields = {"prompt": PROMPT, "audio": obj({"music": TEXT, "sfx": TEXTS, "continuity": TEXT}),
                           "end": STATE, "transition_to_next": {"type": ["string", "null"]}}
        if previous_end is None:
            fragment_fields["start"] = STATE
        schema = obj({"status": STATUS, "issues": TEXTS, "shot": {"anyOf": [obj(fragment_fields), {"type": "null"}]}})
        context = {"operation": "story_assembly", "stage": "shot", "shot_number": index + 1,
                   "is_last": index == len(plan["shots"]) - 1,
                   "locked": {"theme": {k: v for k, v in locked["theme"].items() if k != "reality_mapping"}, "visual_style": locked["visual_style"]},
                   "action_outline": [{"beat_id": beat["beat_id"], "first_person_action": beat["first_person_action"]} for beat in locked["script"]],
                   "current_beats": [beat for beat in locked["script"] if beat["beat_id"] in item["beat_ids"]],
                   "shot_plan": item, "previous_end": previous_end, "audio_plan": plan["audio_plan"],
                   "world_rules": (inputs.get("story_memory") or {}).get("world_rules", []),
                   "production_constraints": inputs["production_constraints"]}
        def validate_fragment(output):
            if not ready(output):
                return output
            exact(output, ("status", "issues", "shot"), "镜头输出")
            shot = build_shot(output["shot"], item, index, len(plan["shots"]), previous_end, locked, inputs)
            partial_locked = {**locked, "script": context["current_beats"]}
            partial_plan = {**plan, "world_delta_draft": []}
            check_shot = {**shot, "transition_to_next": None}
            check_inputs = {**inputs, "production_constraints": {**inputs["production_constraints"], "duration_seconds": item["duration_seconds"]}}
            validate_story(assemble(partial_locked, partial_plan, [check_shot], check_inputs), check_inputs)
            return output
        output = request(root, f"shot-{index + 1:03}", SHOT_PROMPT, context, provider,
                         stage_seed(seed, 0, f"shot-{index + 1:03}"), temperature, validate_fragment, progress,
                         request_options={"schema": schema, "max_tokens": 4096})
        if output["status"] != "ready":
            return output
        shots.append(build_shot(output["shot"], item, index, len(plan["shots"]), previous_end, locked, inputs))
        if shots[-1]["prompt"] != output["shot"]["prompt"]:
            prompt_defaults.append({"shot_id": shots[-1]["shot_id"], "field": "prompt.画面内容",
                                    "directive": VIEWPOINT_DIRECTIVE})
    story = assemble(locked, plan, shots, inputs)
    validate_story(story, inputs)
    write_json(root / "result.json", story)
    write_json(root / "assembly-record.json", {"locked_digests": {key: digest(value) for key, value in locked.items()},
               "story_digest": digest(story), "shot_count": len(shots), "seed": seed,
               "prompt_defaults": prompt_defaults})
    progress("主题、脚本和画风原样保留，分镜已由程序组装。")
    return story
