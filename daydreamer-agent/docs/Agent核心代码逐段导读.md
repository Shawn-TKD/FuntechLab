# 白日梦想家 Agent：核心代码逐段导读

日期：2026-09-29。以下片段直接摘自当前工作区源码；展示顺序按业务执行排列。标明“节选”的函数未展示全部分支，请通过文件链接查看完整实现。本文只记录当前版本，后续源码修改可能导致行号变化。

整体流程、文件树和机制解释见[Agent思路与流程实现分析](<../docs/Agent思路与流程实现分析.md>)。

## 阅读方式

先看01–08理解输入，再看09–18理解创作，19–26理解视频与恢复，27–29理解网页和记忆。代码中的path/run_id对应主分析文档中的任务目录。

## 01. 入口：把本地src接入Python

此文件不承载业务逻辑，只设置模块查找路径并调用cli.main。Windows窗口选文件发生在daydreamer.ps1。

代码：[daydreamer.py:1](<../daydreamer.py#L1>) · 完整入口文件

```python
"""Local entry point; no installation or global PYTHONPATH required."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
from daydreamer_agent.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
```

## 02. 新任务：规范化输入与模型约束

先读取事件卡和记忆，再规范化事件。continuity_mode和video_model写入production_constraints，供后续规划、校验和提交共用。节选之后还会补默认值、校验、保存技能及配置快照。

代码：[src/daydreamer_agent/cli.py:79](<../src/daydreamer_agent/cli.py#L79>) · new_run（节选）

```python
def new_run(args, config, store, *, demo=False):
    if not args.events:
        raise ValidationError("创建任务需要 --events。")
    raw = read_json(args.events)
    memory = load_memory(args.project / config["storage"]["memory_directory"], args.world_id or config["memory"]["world_id"], args.memory)
    production = config["production"]
    inputs = normalize(raw, memory=memory, duration=args.duration, ratio=args.ratio, resolution=args.resolution)
    constraints = inputs["production_constraints"]
    constraints["video_model"] = config["video"]["model"]
    spec = capabilities(config["video"]["model"])
    mode = args.continuity or constraints.get("continuity_mode", config["video"].get("continuity_mode", "frame_chain"))
    if mode not in {"single_take", "frame_chain"}:
        raise ValidationError("continuity_mode 必须为 single_take 或 frame_chain。")
    constraints["continuity_mode"] = mode
    for name, default in (("duration_seconds", production["duration_seconds"]), ("aspect_ratio", config["video"]["aspect_ratio"]), ("resolution", config["video"]["resolution"])):
        if constraints.get(name) is None and default != "":
            constraints[name] = default
```

## 03. 原始视频：持久保存提取与生成的关联

先验证事件卡指纹，再复用或建立generation_run_id，并写source-extraction.json。关联在实际创作之前保存，恢复时沿用相同子任务。此处展示的是函数中段。

代码：[src/daydreamer_agent/application/video_workflow.py:63](<../src/daydreamer_agent/application/video_workflow.py#L63>) · run_video_workflow（节选）

```python
    # Link under the extraction lock before any paid creative/generation call.
    # A crash before this link can leave an unused child, but cannot submit its videos.
    with extraction_store.lock(run_id):
        job = extraction_store.load(run_id)
        card = read_json(path / "card.json")
        if digest(card) != job["card_digest"]:
            raise ValidationError("提取的事件卡已变化，不能接入原生成任务。")
        child_args = copy(args)
        child_args.video = None
        child_args.events = path / "card.json"
        for name, value in job["generation_options"].items():
            setattr(child_args, name, Path(value) if name in {"memory", "audio"} and value else value)
        # Audio may explicitly be supplied on a later resume; otherwise retain the original choice.
        if args.audio:
            child_args.audio = args.audio
        frozen_config = read_json(path / "input/config.json")
        child_id = job.get("generation_run_id")
        if not child_id:
            child_id = create_generation(child_args, frozen_config, generation_store)
            write_json(generation_store.path(child_id) / "input/source-extraction.json", {
                "extraction_run_id": run_id, "card_digest": job["card_digest"],
                "generation_requested_by": "user_selected_video_for_full_workflow",
                "deduplication_performed": False,
            })
            job.update(generation_run_id=child_id, generation_status="created")
            extraction_store.save(run_id, job)
        source = read_json(generation_store.path(child_id) / "input/source-extraction.json")
        if (source["extraction_run_id"] != run_id or source["card_digest"] != job["card_digest"]
                or digest(read_json(generation_store.path(child_id) / "input/original.json")) != job["card_digest"]):
            raise ValidationError("生成任务与事件卡来源不匹配，停止以免混用结果。")
    print("自动进入故事与视频生成，生成任务：" + child_id, flush=True)
    result = generate(child_args, frozen_config, generation_store, media, credentials, child_id)
    with extraction_store.lock(run_id):
        job = extraction_store.load(run_id)
        job["generation_status"] = result["status"]
        extraction_store.save(run_id, job)
    return {**result, "run_id": run_id, "generation_run_id": child_id,
            "event_card": extraction["event_card"], "workflow_stage": "generation"}
```

## 04. 素材分段：避免极短尾段

段数按总时长向上取整，再均分时间。这样61秒不会产生60秒加1秒的短尾段。

代码：[src/daydreamer_agent/event_extraction/media.py:28](<../src/daydreamer_agent/event_extraction/media.py#L28>) · chunk_ranges（完整函数）

```python
def chunk_ranges(duration, seconds):
    # Equal intervals avoid a final chunk shorter than the model's 2-second minimum.
    count = math.ceil(duration / seconds)
    return [(duration * i / count, duration * (i + 1) / count) for i in range(count)]
```

## 05. 视觉理解：观察后再做结构化

真实QwenVision实现observe，因此先读视频生成文字观察，再递归进入structured-v4纯文本结构化阶段。检查点保存协议版本和输入basis。

代码：[src/daydreamer_agent/event_extraction/pipeline.py:45](<../src/daydreamer_agent/event_extraction/pipeline.py#L45>) · EventExtraction.stage（节选）

```python
        if video is not None and callable(getattr(self.provider, "observe", None)):
            # Multimodal requests cannot enforce the nested card schema. Cache
            # grounded observations, then use strict schema in a text-only call.
            observed = self.observe_video(path, basis, context, video, fps, name)
            if observed["status"] != "ready":
                result = {"status": observed["status"], "issues": observed["issues"], "card": None}
            else:
                self.progress("视频观察已保存，正在整理生活事件卡：" + name)
                result = self.stage(run_id, name + "/structured-v4", {
                    **context, "visual_observations": observed["observations"],
                    "task": "仅将visual_observations整理成事件卡；不添加观察未提供的事实或时间。引用仅使用给定素材ID及观察中的时间。若缺少必要依据返回needs_resolution，不能编造。"
                }, rules, fps=fps)
            validate_response(result, rules["schema"], context)
            write_json(checkpoint, {"basis": basis, "result": result, "extraction_protocol": "observation_then_schema_v4"})
            return result
```

## 06. 多段时间戳：映射回原素材

模型最初给的是片段内时间。程序给关键画面和动作区间加上该段起点，后面的合并才能使用统一的原视频时间。

代码：[src/daydreamer_agent/event_extraction/pipeline.py:187](<../src/daydreamer_agent/event_extraction/pipeline.py#L187>) · EventExtraction.run（节选）

```python
                    if result["status"] == "ready":
                        card = deepcopy(result["card"])
                        for frame in card["key_frames"]:
                            frame["timestamp_seconds"] += start
                        for step in card["event_description"]["sequence"]:
                            for ref in step["source_refs"]:
                                ref["start_seconds"] += start
                                ref["end_seconds"] = min(end, ref["end_seconds"] + start)
                        cards.append(card)
```

## 07. 统一事件卡输入

输入既可以是一张卡，也可以是数组或event_cards对象。内部统一为事件卡数组，并要求至少一张卡。后半段还负责唯一ID、数组字段和制作参数校验。

代码：[src/daydreamer_agent/domain/events.py:45](<../src/daydreamer_agent/domain/events.py#L45>) · normalize（节选）

```python
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
```

## 08. 记忆：保留叙事，过滤制作字段

递归删除画风、镜头和提示词等字段，避免直接沿用旧作品的视觉方案。代码只过滤列出的键，不等于清除所有文本中的风格描述。

代码：[src/daydreamer_agent/memory/repository.py:23](<../src/daydreamer_agent/memory/repository.py#L23>) · narrative_memory（完整函数）

```python
def narrative_memory(memory):
    """Remove obvious production fields; remaining prose is governed by skill rules."""
    excluded = {"visual_style", "style", "palette", "lighting", "materials", "prompt", "prompts", "shots", "video_request"}
    if isinstance(memory, dict):
        return {k: narrative_memory(v) for k, v in memory.items() if k not in excluded}
    if isinstance(memory, list):
        return [narrative_memory(v) for v in memory]
    return deepcopy(memory)
```

## 09. 随机种子：创建时保存

默认enabled=true时写入seed_only快照。关闭该配置会使任务没有creativity.json，从而走generator.py旧路径，不是只把随机性设为零。

代码：[src/daydreamer_agent/story/creativity.py:63](<../src/daydreamer_agent/story/creativity.py#L63>) · initialize（完整函数）

```python
def initialize(store, run_id, config):
    settings = policy(config)
    if not settings["enabled"]:
        return
    write_json(store.path(run_id) / "input/creativity.json", {
        "version": 2, "mode": "seed_only", "seed": secrets.randbelow(2**31),
        "policy": {"enabled": True, "temperature": settings["temperature"]},
    })
```

## 10. 随机种子：按阶段派生

由基础seed和阶段标识计算稳定的整数值。它保证请求条件可记录，不能保证外部模型服务重新生成完全相同的内容。

代码：[src/daydreamer_agent/story/creativity.py:73](<../src/daydreamer_agent/story/creativity.py#L73>) · stage_seed（完整函数）

```python
def stage_seed(base, round_index, stage, attempt=0):
    return int(digest([base, round_index, stage, attempt])[:16], 16) % 2**31
```

## 11. 主Pipeline如何选择故事实现

存在creativity.json就走generate_seeded_story。非ready故事会保存状态并停止，只有ready故事进入finish_story。

代码：[src/daydreamer_agent/application/pipeline.py:26](<../src/daydreamer_agent/application/pipeline.py#L26>) · Pipeline.plan（完整函数）

```python
    def plan(self, run_id):
        with self.store.lock(run_id):
            path = self.store.path(run_id)
            job = self.store.load(run_id)
            if job["status"] in {"story_ready", "video_generating", "awaiting_audio", "composing", "completed"}:
                self.verified_story(run_id)
                return job
            self.store.status(run_id, "story_generating")
            try:
                inputs = read_json(path / "input/normalized.json")
                skill = read_json(path / "input/skill.json")
                generate = generate_seeded_story if (path / "input/creativity.json").exists() else generate_story
                story = generate(path, inputs, skill, self.story_provider, self.progress)
                if story["status"] != "ready":
                    write_json(path / "story/result.json", story)
                    self.store.status(run_id, story["status"])
                else:
                    self.finish_story(run_id, story)
            except Exception:
                self.store.status(run_id, "failed", failed_stage="story")
                raise
            return self.store.load(run_id)
```

## 12. 按顺序生成三个创作阶段

STAGES[:3]依次为theme、script、visual_style；prior持续累积上下文。validate_candidates在这里用于验证一个方案，不意味着默认又启用了多候选流程。三阶段完成后转入generate_assembly。

代码：[src/daydreamer_agent/story/seeded.py:116](<../src/daydreamer_agent/story/seeded.py#L116>) · generate_attempt（完整函数）

```python
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
```

## 13. 创作请求：指纹与检查点

相同basis的已完成检查点直接复用；basis不一致则拒绝继续。两次循环代表首次生成加一次修正。完整函数还保存错误、原响应、token用量和耗时。

代码：[src/daydreamer_agent/story/diversity.py:103](<../src/daydreamer_agent/story/diversity.py#L103>) · request（节选）

```python
def request(path, name, instruction, context, provider, seed, temperature, validate, progress, *, request_options=None):
    basis_data = {"instruction": instruction, "context": context, "seed": seed, "temperature": temperature}
    if request_options:
        basis_data["request_options"] = request_options
    basis = digest(basis_data)
    checkpoint = path / f"{name}.json"
    if checkpoint.exists():
        saved = read_json(checkpoint)
        if saved["basis"] != basis:
            raise ValidationError("创作检查点规则发生变化，请新建任务。")
        return validate(saved["output"])
    repair = None
    for attempt in range(2):
        response_path = path / "responses" / f"{name}-{attempt + 1}.json"
        attempt_seed = (seed + attempt) % 2**31
        if response_path.exists():
            response = read_json(response_path)
            if response["basis"] != basis:
                raise ValidationError("已保存的创作响应与当前输入不匹配。")
        else:
            progress("正在创作：" + name + ("（修正格式）" if repair else ""))
```

## 14. 锁定主题、脚本与画风后规划分镜

locked使用deepcopy保留前面三个阶段。规划请求使用PLAN_SCHEMA和8192输出token上限；之后本地配平时长，另存plan-timing.json。

代码：[src/daydreamer_agent/story/assembly.py:168](<../src/daydreamer_agent/story/assembly.py#L168>) · generate_assembly（节选）

```python
def generate_assembly(stage_path, inputs, prior, provider, seed, temperature, progress):
    root = stage_path / "assembly-v1"
    locked = {key: deepcopy(prior[key][key]) for key in ("theme", "script", "visual_style")}
    # Creative outputs are provided once as authoritative context, never requested as output.
    plan_context = {"operation": "story_assembly", "stage": "story_plan", "locked": locked,
                    "production_constraints": inputs["production_constraints"],
                    "story_memory": inputs.get("story_memory"), "current_preferences": inputs.get("current_preferences"),
                    "events": [{key: card[key] for key in ("event_id", "summary", "unknowns") if key in card} for card in inputs["event_cards"]]}
    plan_prompt = PLAN_PROMPT.replace("每镜3至15秒", "每镜4至15秒") if minimum_duration(inputs["production_constraints"]) == 4 else PLAN_PROMPT
    plan = request(root, "plan", plan_prompt, plan_context, provider, stage_seed(seed, 0, "story_plan"), temperature,
                   lambda output: validate_plan(output, locked, inputs), progress,
                   request_options={"schema": PLAN_SCHEMA, "max_tokens": 8192})
    if plan["status"] != "ready":
        return plan
    proposed = [shot["duration_seconds"] for shot in plan["shots"]]
    proposed_memory = deepcopy(plan["used_memory_ids"])
    plan = prepared_plan(plan, inputs)
    allocated = [shot["duration_seconds"] for shot in plan["shots"]]
    write_json(root / "plan-timing.json", {"version": 1, "proposed_seconds": proposed,
               "allocated_seconds": allocated, "target_seconds": inputs["production_constraints"].get("duration_seconds"),
               "max_seconds": inputs["production_constraints"].get("max_duration_seconds"),
               "actual_seconds": sum(allocated) if all(value is not None for value in allocated) else None,
               "proposed_memory_ids": proposed_memory, "used_memory_ids": plan["used_memory_ids"]})
```

## 15. 时长分配：从最小值开始逐秒分配

前面的代码先检查目标总长和镜头数是否可行。这里按建议比例计算desired，每次把1秒分给最接近目标比例且未达15秒的镜头，最后得到总和正确的整数时长。

代码：[src/daydreamer_agent/story/timing.py:67](<../src/daydreamer_agent/story/timing.py#L67>) · fit_plan_timing（节选）

```python
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
```

## 16. 后镜开始状态直接继承前镜结束状态

第一镜使用模型提供的start；后续镜头深拷贝previous_end。STATE_FIELDS规定六个连续状态字段。这是文本结构层的连续性，并非视觉效果已经验证。

代码：[src/daydreamer_agent/story/assembly.py:139](<../src/daydreamer_agent/story/assembly.py#L139>) · build_shot（节选）

```python
def build_shot(fragment, item, index, count, previous_end, locked, inputs):
    keys = ("prompt", "audio", "end", "transition_to_next") + (() if previous_end is not None else ("start",))
    exact(fragment, keys, "单镜头")
    start = deepcopy(previous_end if previous_end is not None else fragment["start"])
    end = fragment["end"]
```

## 17. 完整故事由程序组装

主题、脚本、画风来自locked；来源ID、记忆基线、声音固定约束和checks空数组由程序补齐。模型不会在此获得重写锁定字段的机会。

代码：[src/daydreamer_agent/story/assembly.py:127](<../src/daydreamer_agent/story/assembly.py#L127>) · assemble（完整函数）

```python
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
```

## 18. 视频提示词包含行动上下文

编译器把持续目标、前段进展、当前脚本和起止状态带入每镜请求。余下部分继续加入统一画风、世界约束、连续状态和四项分镜提示词。

代码：[src/daydreamer_agent/prompts/video_compiler.py:14](<../src/daydreamer_agent/prompts/video_compiler.py#L14>) · compile_shot（节选）

```python
def compile_shot(story, shot, constraints, model):
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
```

## 19. 一键流程如何串起计划、生成和拼接

有故事指纹则验证复用，否则先plan。只有所有镜头都已达到awaiting_audio，才进入compose；没有音频清单时明确使用preview模式。

代码：[src/daydreamer_agent/application/pipeline.py:108](<../src/daydreamer_agent/application/pipeline.py#L108>) · Pipeline.run_full（完整函数）

```python
    def run_full(self, run_id, *, audio_manifest=None, wait_seconds=3600):
        """Run or resume each stage, stopping on any unresolved state."""
        job = self.store.load(run_id)
        if job.get("story_digest"):
            self.verified_story(run_id)
        else:
            job = self.plan(run_id)
        if job["status"] == "completed":
            return self.compose(run_id, audio_manifest) if audio_manifest else job
        if not job.get("story_digest"):
            return job
        job = self.render(run_id, wait_seconds=wait_seconds)
        if job["status"] != "awaiting_audio":
            return job
        return self.compose(run_id, audio_manifest, preview=audio_manifest is None)
```

## 20. 远端提交前先保存本地状态

在HTTP请求前先持久化submitting；明确拿到任务ID后保存submitted。ProviderError.uncertain决定是否进入submission_unknown，防止在不确定时盲目重发。

代码：[src/daydreamer_agent/application/pipeline.py:124](<../src/daydreamer_agent/application/pipeline.py#L124>) · Pipeline.submit_attempt（完整函数）

```python
    def submit_attempt(self, run_id, state, request, predecessor, frame):
        path = self.store.path(run_id)
        attempt_path = path / "shots" / state["shot_id"] / "attempts" / str(state["attempt"])
        payload = prepare_submission(request, frame, require_first_frame=predecessor is not None)
        write_json(attempt_path / "video_request.json", {"request": request, "predecessor": predecessor,
                   "first_frame": str(frame.relative_to(path)) if frame else None})
        if predecessor:
            state["predecessor"] = predecessor
        state.update(status="submitting", submission_started_at=time.time())
        self.store.save_shot(run_id, state)
        self.progress("提交镜头：" + state["shot_id"])
        try:
            state["task_id"] = self.video_provider.submit(payload)
        except ProviderError as exc:
            state["status"] = "submission_unknown" if exc.uncertain else "failed"
            self.store.save_shot(run_id, state)
            self.store.status(run_id, state["status"], failed_stage="video", failed_shot=state["shot_id"])
            raise
        state.update(status="submitted", submitted_at=time.time())
        self.store.save_shot(run_id, state)
        return attempt_path / "clip.mp4"
```

## 21. 根据原片和参数缓存剪辑与末帧

prepared_basis绑定原片指纹、制作参数和处理版本。缓存有效还要同时满足prepared.mp4和tail.png的文件指纹一致。

代码：[src/daydreamer_agent/application/pipeline.py:87](<../src/daydreamer_agent/application/pipeline.py#L87>) · Pipeline.prepare_boundary（完整函数）

```python
    def prepare_boundary(self, run_id, state, parameters):
        path = self.store.path(run_id)
        clip = path / state["clip"]
        prepared = clip.with_name("prepared.mp4")
        tail = clip.with_name("tail.png")
        basis = digest({"source_sha256": state["sha256"], "parameters": parameters, "preparation_version": "1.0"})
        valid = state.get("prepared_basis") == basis and prepared.exists() and tail.exists()
        if valid:
            valid = file_hash(prepared) == state.get("prepared_sha256") and file_hash(tail) == state.get("tail_sha256")
        if not valid:
            self.media.prepare_clip(clip, prepared, parameters)
            self.media.extract_tail(prepared, tail, parameters["duration"])
            state.update(prepared_clip=str(prepared.relative_to(path)), prepared_sha256=file_hash(prepared),
                         tail_frame=str(tail.relative_to(path)), tail_sha256=file_hash(tail), prepared_basis=basis)
            self.store.save_shot(run_id, state)
```

## 22. 从实际剪辑文件提取最后一帧

从文件末尾约1秒开始解码，-update 1不断覆盖同一图片，留下最后解码帧；不按目标秒数推算帧号。

代码：[src/daydreamer_agent/media/ffmpeg.py:124](<../src/daydreamer_agent/media/ffmpeg.py#L124>) · Media.extract_tail（完整函数）

```python
    def extract_tail(self, prepared_clip, target, duration):
        # Keep the final decoded frame, including when the edited duration has
        # an allowed rounding error. Do not infer its index from target seconds.
        target = Path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.stem + ".part.png")
        self.execute([self.ffmpeg, "-nostdin", "-y", "-v", "error", "-protocol_whitelist", "file,pipe", "-sseof", "-1", "-i", str(Path(prepared_clip).resolve()),
                      "-map", "0:v:0", "-an", "-fps_mode", "passthrough", "-update", "1", str(temporary)])
        if not temporary.exists() or temporary.stat().st_size == 0:
            raise ValidationError("未能提取实际剪辑末帧。")
        temporary.replace(target)
```

## 23. H3与Wan能力集中定义

模型的最短单镜时长、支持分辨率和提示词长度在同一处声明。H3只开放768P是当前合成链的本地限制。

代码：[src/daydreamer_agent/providers/video_models.py:8](<../src/daydreamer_agent/providers/video_models.py#L8>) · capabilities（完整函数）

```python
def capabilities(model):
    if model == WAN:
        return {"minimum": 3, "resolutions": {"480P", "720P", "1080P"}, "prompt_limit": 20000}
    if model == H3:
        # The current compositor uses short-edge P resolutions. 2K needs a
        # separate output-dimension contract before it can be exposed here.
        return {"minimum": 4, "resolutions": {"768P"}, "prompt_limit": 7000}
    raise ValidationError("不支持的视频模型：" + str(model))
```

## 24. 镜头状态同时写当前记录和尝试历史

state.json表示当前采用的尝试，attempts/<n>/task.json保留每次尝试。SQLite记录便于查询，核心恢复依据仍为JSON。

代码：[src/daydreamer_agent/storage/jobs_sqlite.py:78](<../src/daydreamer_agent/storage/jobs_sqlite.py#L78>) · JobStore.save_shot（完整函数）

```python
    def save_shot(self, run_id, shot):
        path = self.path(run_id) / "shots" / identifier(shot["shot_id"])
        write_json(path / "state.json", shot)
        write_json(path / "attempts" / str(shot["attempt"]) / "task.json", shot)
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO shots VALUES (?,?,?,?)", (run_id, shot["shot_id"], shot["status"], shot.get("task_id")))
        self.audit(run_id, "shot_status", shot_id=shot["shot_id"], status=shot["status"], attempt=shot["attempt"])
```

## 25. JSON原子写入

临时文件与目标在同一目录，写入并刷新后原子替换，避免中断后留下半份JSON。finally负责清理未使用的临时文件。

代码：[src/daydreamer_agent/storage/files.py:40](<../src/daydreamer_agent/storage/files.py#L40>) · write_json（完整函数）

```python
def write_json(path, value):
    path = Path(path)
    data = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".writing-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
```

## 26. 导出前检查结果是否可用

检查输出路径边界、outputs_stale标志、文件存在性及SHA-256。后面才复制视频并生成故事JSON、可读Markdown和运行记录。

代码：[src/daydreamer_agent/application/delivery.py:9](<../src/daydreamer_agent/application/delivery.py#L9>) · export_delivery（节选）

```python
def export_delivery(run_path, output, *, preview=True):
    run_path, output = Path(run_path).resolve(), Path(output).resolve()
    manifest = read_json(run_path / ("composition/preview-manifest.json" if preview else "manifest.json"))
    source = (run_path / manifest["preview" if preview else "final"]).resolve()
    job = read_json(run_path / "job.json")
    if (not source.is_relative_to(run_path) or job.get("outputs_stale")
            or not source.is_file() or file_hash(source) != manifest["sha256"]):
        raise ValidationError("导出视频缺失、过期或已变化，请恢复原任务。")
    story = read_json(run_path / "story/result.json")
    from daydreamer_agent.story.creativity import verify_decision
    verify_decision(run_path, story)
    output.mkdir(parents=True, exist_ok=True)
    video = output / ("连续续接_无声预览.mp4" if preview else "成片.mp4")
    temporary = video.with_suffix(".part.mp4")
    shutil.copyfile(source, temporary)
    temporary.replace(video)
    story_path = output / "完整故事与分镜.json"
```

## 27. 网页Worker复用命令行流程

先保存提取编号，再调用原有run_command；网页只追加队列和结果状态。ready可以对应无声预览，不要求核心生成任务已达到正式completed。

代码：[src/daydreamer_agent/web/worker.py:42](<../src/daydreamer_agent/web/worker.py#L42>) · process（完整函数）

```python
def process(store, job, project):
    try:
        # Persist the extraction id before the first API call. A crash here cannot
        # cause paid work to be submitted under a new id on service restart.
        run_id = job["run_id"]
        if not run_id:
            config = load_settings(project)
            extraction = JobStore(project / "data/extractions", project / "data/extractions.sqlite3")
            run_id = EventExtraction(project, extraction, Media(project)).create(Path(job["source"]), config)
            store.update(job["id"], run_id=run_id)
        args = parser().parse_args(["--project", str(project), "run", "--run", run_id, "--wait", "3600"])
        with contextlib.redirect_stdout(Progress(store, job["id"])):
            result = run_command(args)
        if result.get("status") in {"preview_ready", "completed"}:
            video = Path(result["delivery"]["video"]).resolve()
            if not video.is_relative_to(project / "outputs") or not video.is_file():
                raise ValueError("Unexpected delivery path")
            store.update(job["id"], state="ready", video=str(video), message="幻想片段已准备好")
        else:
            store.update(job["id"], state="paused", message="任务需要继续处理或补充素材，可恢复原任务。")
    except AgentError as exc:
        store.update(job["id"], state="failed", message=str(exc)[:500])
    except Exception as exc:
        # Do not send provider payloads, paths, or credentials to the client/log.
        print("Worker failure:", type(exc).__name__, file=sys.stderr, flush=True)
        store.update(job["id"], state="failed", message="处理暂时中断，已保留任务进度。")
```

## 28. 网页队列以事务领取任务

BEGIN IMMEDIATE包住查找和标记processing，避免多处同时领取同一个queued任务；worker本身还持有独占文件锁。

代码：[src/daydreamer_agent/web/store.py:91](<../src/daydreamer_agent/web/store.py#L91>) · Store.claim（完整函数）

```python
    def claim(self):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM jobs WHERE state='queued' ORDER BY created LIMIT 1").fetchone()
            if row:
                db.execute("UPDATE jobs SET state='processing', updated=?, message='正在检查视频' WHERE id=?", (time.time(), row["id"]))
                return dict(row)
```

## 29. 长期记忆目前只写草案

committed固定为False。代码没有把草案自动合并进data/memory/<world_id>/current.json。

代码：[src/daydreamer_agent/memory/repository.py:33](<../src/daydreamer_agent/memory/repository.py#L33>) · save_draft（完整函数）

```python
def save_draft(run_path, story):
    write_json(Path(run_path) / "story/world_delta_draft.json", {
        "committed": False, "memory_basis": story.get("memory_basis"),
        "changes": story.get("world_delta_draft", []),
    })
```

## 补充：默认链的真实调用关系

```text
cli.run_command
  ├─ 原视频 → run_video_workflow
  │    ├─ EventExtraction.create / run
  │    ├─ cli.new_run（创建或复用关联任务）
  │    └─ cli.run_generation
  └─ 事件卡 → cli.new_run → cli.run_generation
       └─ Pipeline.run_full
            ├─ Pipeline.plan
            │    └─ generate_seeded_story
            │         └─ generate_attempt
            │              ├─ request(theme)
            │              ├─ request(script)
            │              ├─ request(visual_style)
            │              └─ generate_assembly
            │                   ├─ request(plan) → fit_plan_timing
            │                   ├─ request(shot-001...N) → build_shot
            │                   └─ assemble → validate_story
            │    └─ finish_story → compile_shot / save_draft
            ├─ Pipeline.render
            │    ├─ submit_attempt → BailianVideo.submit
            │    ├─ query / download / accept_downloaded_clip
            │    └─ prepare_boundary → prepare_clip / extract_tail
            └─ Pipeline.compose → Media.compose
       └─ export_delivery
```

图中的generate_seeded_story是当前默认路径；没有creativity.json的任务使用generator.generate_story兼容路径。真实视频调用只会在run或render进入视频阶段时发生；本次文档分析没有执行这些付费入口。
