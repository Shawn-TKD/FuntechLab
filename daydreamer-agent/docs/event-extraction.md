# 视频生活事件卡提取

本模块将一段围绕同一事件的视频整理成一张七项生活事件卡。提取阶段不使用 daydreamer-style-transfer，不调用 Wan。声音不分析，关键画面只存文字，不产生截图。默认一键入口会在提取成功后自动接入幻想创作及Wan生成；`-Extract` 只提取事件卡。

## 入口与文件

`daydreamer.cmd -Extract` 弹窗选视频；`-Extract "视频路径"` 直接读取；`-Extract -Run 编号` 恢复。

Python入口：`daydreamer.py extract --video 视频路径` 或 `extract --run 编号`。

| 文件 | 职责 |
| --- | --- |
| `prompts/event_extraction.md` | 中文事实提取规则、缺失信息和多事件处理 |
| `schemas/life_event_card.schema.json` | 完整七项卡片的结构化输出约束 |
| `src/daydreamer_agent/providers/qwen_vision.py` | 实际视频字节上传、JSON Schema参数、响应读取 |
| `src/daydreamer_agent/event_extraction/media.py` | 媒体元数据、分段和无声转码 |
| `src/daydreamer_agent/event_extraction/validation.py` | 本项目Schema子集校验与跨字段、引用、时间规则 |
| `src/daydreamer_agent/event_extraction/pipeline.py` | 任务快照、分段理解、合并、修复与导出 |
| `resources/event-card/` | 用户提供的空模板与v0.2格式文档快照 |

现有虚拟环境和FFmpeg直接复用，无新增Python依赖。Schema校验器只实现本项目使用的类型、枚举、anyOf、对象必填/额外字段与数组项规则；它不是通用JSON Schema解释器。扩展Schema关键字时须同步校验器及测试。

## 执行流程

1. 读取视频轨时长、尺寸、音轨存在性及文件SHA-256。不把容器创建时间当作实际事件时间。
2. 创建独立提取任务，保存素材路径、参数、提示词和Schema快照。原文件保持原样。
3. 默认每段不超过60秒，均分避免不足2秒的尾段。转为最长边640像素、12fps、无音轨的MP4副本，不改变播放速度或剪去黑屏。模型默认每秒观察2帧。每段转码后检查时长、大小、无音轨；编码前最多6,750,000字节，Base64后保守低于10MB。
4. 通过Qwen视频输入传入副本。模型按段内秒数提取，程序做范围检查后加偏移量，统一映射至原视频。
5. 单个有效段直接采用；多个有效段用同一模型根据分段文字合并。合并不能移动关键画面时间或扩展输入步骤的证据区间。无事件段保留在处理记录；多件独立事件返回needs_resolution，不强行合并。
6. 校验通过后保存卡片、素材引用表、提取记录。模型原始文本响应与usage留在任务内部，不保存密钥或Base64请求正文。

## 结构化输出与系统字段

API使用 `response_format.type=json_schema`、`strict=true`、`enable_thinking=false`。接口外层包裹 `{status, issues, card}`，用于区分ready、no_event、needs_resolution；导出的事件卡只有原来的七项字段，不含外层控制字段。

每阶段最多两次模型返回：首次输出加一次带具体错误的修正。响应先写盘再校验，完整响应截断、JSON错误、引用越界等均不得当作成功。恢复复用响应与检查点，修正预算跨恢复保留；连接失败不悄悄切换格式或模型。

关键画面时间使用半开区间：`0 <= timestamp_seconds < duration_seconds`，动作区间的结束时间可以等于视频总时长。程序不会把越界时间自动改小。错误信息包含数组下标、具体字段、当前值和允许范围；可空card的对象校验失败时保留内部错误，不再用笼统的anyOf类型错误覆盖它。

修正请求携带完整上一版输出及field、expected、保留有效内容的要求，禁止用空对象省略条目；仍执行完整结构、引用与时间检查。旧任务若保存的两次响应均来自升级前，且首次失败在关键画面时间范围，允许一次带精确反馈的升级修正，使用首次完整响应作为基础，记录为`response-repair-v2.json`，原响应不变。该额外机会跨恢复保留，不适用于新任务或其他错误，也不会在部署时自动触发模型调用。

事件ID与素材ID由程序生成，画面记录ID从程序给出的列表选取。`image_uri=null`、`audio_info=[]`、实际发生时间=null、去重结果=null、记忆数组为空、`waiting_accumulation` 均由本地校验强制执行。`details_to_preserve` 引用本卡的文字画面记录或动作顺序。

结构约束和引用检查无法保证模型描述完全准确。保留细节需要真实依据，时间为模型估计位置。提取记录标明声音未分析、未人工复核、未去重，不能把未分析声音写成无音轨。

## 保存与恢复

```text
data/extractions/<run_id>/
  input/                 # 素材元数据、配置、提示词、Schema快照
  media/                 # 无声理解用视频分段及指纹；没有图片
  stages/                # 每段及合并的响应、校验检查点
  observations.json      # 全部已处理区间与结果
  card.json              # 校验通过的最终事件卡
  job.json               # 状态、卡片指纹
outputs/event-cards/<run_id>/
  event_<run_id>.json
  素材引用表.json
  提取记录.json
```

使用独立提取目录和SQLite索引；复用现有进程锁阻止同任务同时写入。处理中原视频变更会停止；`-Extract` 已完成任务重导出无需原视频或模型密钥。

## 与默认一键入口衔接

`daydreamer.cmd` 默认选择原视频；也可直接传视频路径。底层执行 `run --video`，由 `application/video_workflow.py` 串联提取与原有生成流水线。只有提取完成且事件卡校验通过后才创建生成任务；no_event或needs_resolution不会提交故事或视频模型。

开始即打印统一恢复命令 `daydreamer.cmd -Run <全流程编号>`。提取任务保存制作参数、配置和 `generation_run_id`。关联在加锁状态下、任何付费创作调用之前持久化；恢复复用原生成任务。生成任务记录原事件卡指纹和提取编号；关联不符时停止。既有生成任务编号仍能按原方式恢复。

卡片中去重、记忆和筛选字段保持提取事实，不虚构去重成功。本次用户明确选择视频执行全流程的意图独立记录。最终视频目录附带事件卡及来源关联，原始素材提取记录仍留在事件卡导出目录。只想提取卡片时继续使用 `-Extract`。

## 接口依据

- [百炼视觉理解](https://help.aliyun.com/zh/model-studio/vision)：视频Data URL、fps、2秒最低时长、Base64小于10MB，以及视觉接口不分析视频音频。
- [百炼结构化输出](https://help.aliyun.com/zh/model-studio/qwen-structured-output)：Qwen3.8-Max支持严格JSON Schema。

应用默认30分钟输入上限属于本地流程设置，不是模型服务的最长时长声明。
