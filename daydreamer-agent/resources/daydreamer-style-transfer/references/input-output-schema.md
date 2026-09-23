# 输入输出约定

这是本 skill 的建议接口，schema_version 为 `1.0`，不是外部服务的既定协议。接受语义等价的生活卡或自然语言输入，在内部规范化，不要求用户补齐所有空字段。

## 输入

| 字段 | 内容与缺失处理 |
| --- | --- |
| event_cards | 生活卡数组：event_id、summary、ordered_actions、details、source_refs、sound_clues、unknowns。至少需要可理解的事件或细节；无 ID 时可分配本次局部 ID，注明为局部标识 |
| story_memory | 可选：world_id、version、world_rules、relationships、places、objects、current_state、recent_plots。保留原有 ID；缺失时为空，不声称检索到历史 |
| production_constraints | 可选：duration_seconds、aspect_ratio、shot_limit、model_capabilities。未指定的值用 null；仅建议的数值明确标为 proposal |
| current_preferences | 本次明确表达的风格或内容偏好；不得从历史画风自动填入。含写实倾向时转成非写实方案，并在 assumptions 记录调整 |

source_refs 只记录输入中实际提供的文件、关键帧、素材 ID 或时间位置；没有来源定位时保留事件引用，source_refs 为空。sound_clues 中的语音允许帮助理解，不可作为成片人声。

## 输出方式与状态

系统集成或用户要求 JSON 时输出合法 JSON，不夹注释、占位符或额外说明。一般人工创作请求默认以同名栏目给出可读结果。两种形式语义一致。使用以下顶层字段：

| 字段 | 类型／约定 |
| --- | --- |
| schema_version | 字符串 `1.0` |
| status | `ready`、`wait_for_material` 或 `needs_resolution` |
| source_event_ids | 使用的事件 ID 数组 |
| memory_basis | 对象：world_id、version，可为 null；used_ids 数组仅含实际引用的记忆 ID |
| assumptions | 建议的制作参数、局部标识或创作前提数组；不将幻想设定伪装成事实 |
| theme | 成功时为主题对象，其他状态可为 null |
| script | 成功时为按顺序排列的段落数组，其他状态为空数组 |
| visual_style | 成功时为风格对象，其他状态可为 null |
| audio_plan | 成功时为声音对象，其他状态可为 null |
| shots | 成功时为分镜数组，其他状态为空数组 |
| world_delta_draft | 可选增量数组，无增量时为空；不代表已提交 |
| checks | 兼容字段，当前固定为空数组；不输出生成后核查、自评或 pass 结论 |
| issues | 缺失或冲突数组：field、reason、needed。正常结果为空数组 |

`ready` 仅代表文本创作结果可以交接，不代表媒体已生成或通过实机验证。关键创作信息不足时用 `wait_for_material`；相互矛盾且无法遵守的世界事实或当前约束用 `needs_resolution`。模型、时长、画幅未知或没有历史记忆，通常不构成阻塞。

## 成功结果的对象字段

### theme

- `title`：简短标题。
- `statement`：一句话说明谁正在对谁或什么做什么，明确行动情境，而非仅命名景观。
- `action_goal`：本次新创作填写的持续行动目标；追逐、交战或其他行为服务同一目标。旧故事导入可缺省，Agent 使用 statement 作为兼容描述。
- `core_rule`：本次行动必要的幻想规则，说明作用对象、触发条件、作用方式与边界，以及是沿用记忆还是本次候选。其他部分遵守支撑、接触、碰撞、承载与空间连通的常识。
- `development`：围绕同一目标连续发生的动作链；没有反转，不强制完成整场事件。
- `boundary`：本次主题范围及不加入的无关方向。
- `reality_mapping`：数组，每项包含 event_id、real_detail、fantasy_translation、retained_feature、source_refs。输入无法提供来源定位时 source_refs 为空。
- `contrast_and_interest`：普通生活与幻想经历形成何种反差或趣味。

### script

每段包含 `beat_id`、`source_event_ids`、`first_person_action`、`visible_change`、`end_state`。first_person_action 是我为持续目标采取的具体行动，visible_change 是行动或既定规则的可见后果，end_state 包括位置、目标/障碍状态、已完成与待继续的动作。后段继承进展，不重置目标或重复行动。幻想拓展不改写输入卡的 unknowns；不以解说、无因果环境变形或比喻命名代替情节。片段在自然动作节点结束，允许整场任务继续。

### visual_style

包含 `medium`、`form_and_space`、`palette`、`materials`、`lighting`、`motion_character`、`story_based_reason`、`stable_constraints`（数组）、`transitions`（数组）。无风格切换时 transitions 为空。理由指向本次情节，不引用历史画风作为选取依据。

medium 必须明确为非写实媒介，其他字段不得引导写实、微写实、半写实或实拍表现。stable_constraints 必须包含全画面统一非写实、禁止写实或微写实元素的约束，覆盖双手、身体局部、环境和过渡。每镜 prompt 的“画面内容”同时写出具体非写实特征与该限制，保持原有四项结构，无需新增字段。

form_and_space 明确目标、路径、支撑和遮挡关系；非写实不要求完全扁平化或放弃空间逻辑。美术媒介不自动成为世界里的物理规则。

### audio_plan

包含 `music_direction`、`sound_motifs`（数组）及 `speech_policy`。speech_policy 明确仅背景音乐与音效，无旁白、对白、演唱、人声采样或原始人声音轨。

### shots

每镜包含：

- `shot_id`、`beat_ids`、`source_event_ids`、`source_refs`。
- `duration_seconds`：已知约束下的时长，或 null；另用 `duration_basis` 标记 confirmed、proposal、unspecified，不将建议写成确定配置。
- `start_state`、`end_state`、`transition_to_next`：包含剧情进展的起止状态及动作承接；最后一镜 transition_to_next 为 null，但不要求整场事件结束。不要仅记录车筐、镜头或背景位置。
- `prompt`：只有下列四个键，不把配乐或参数增加为第五项。
- `audio`：music、sfx、continuity，记录本镜声音及跨镜衔接，无对白文本。

```json
{
  "景别": "眼前门锁近景",
  "构图": "门锁在视野中央偏右，自己的右手从下方进入，门框向上延伸",
  "运镜手法": "第一人称探身靠近门锁，视线略微向下，触碰前停稳",
  "画面内容": "第一人称视野中，卡通化手指沿铜锁凹槽滑动，锁内夸张造型的齿轮逐个转动；二维描边动画、简化色块、深青背景与琥珀色平涂明暗突出齿轮间的连接，结束时凹槽旁的小闩抬起。全画面统一非写实，无写实或微写实元素"
}
```

以上 JSON 仅演示 prompt 对象的四项格式，不规定默认故事或风格。

### world_delta_draft

每项包含 `change_type`（new_setting／state_change／plot_record）、`target_id`（新对象无正式 ID 时为 null）、`description`、`basis_beat_ids`、`base_memory_version`。说明本次的新增或变化；不要自动把所有局部创意固化为永久规则。视觉方案留在片段输出中，不作为下次选风格的默认记忆。

## 生成与交接约定

生成时直接遵守上述约束，不增加事后内容核查、评分、审核报告或因自评分数触发的重写。checks 固定为空数组。Agent 保留已有的本地 JSON、引用、时长和状态契约校验及有限格式修复，不调用模型审阅故事或生成媒体。

每镜仅通过 beat_ids 关联它实际推进的脚本段落。Agent 将持续目标、前镜结束进展、当前关联段落和本镜动作范围传入视频提示词；不传入后续段落让视频模型提前演完。一个 beat 跨镜时，以本镜 start_state/end_state 限定执行范围，前镜发生的动作仅作背景，不重演。
