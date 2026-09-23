# 白日梦想家 · 视频生成 Agent

从原始生活视频或生活事件卡 JSON 出发，结合故事记忆，生成第一人称幻想故事、分镜和视频，完成剪辑、声音合成与本地归档。

## 默认一键全流程

在 PowerShell 中运行（任意目录均可）：

```powershell
& "C:\Users\Eldon\Downloads\Bold Maker\daydreamer-agent\daydreamer.cmd"
```

弹窗默认选择原始生活视频，随后自动执行：**理解视频 → 保存生活事件卡 → 创作故事与分镜 → 逐段生成视频 → 拼接 → 保存**。无需再次选择生成的JSON，也无需手动切换模块。取消选取不会创建任务。

文件类型菜单也可切换到生活事件卡JSON，直接从故事创作开始。原有JSON路径用法保持可用。

也可以直接传入文件路径。在项目目录中：

```powershell
.\daydreamer.cmd "C:\素材目录\生活视频.mp4"

# 已有事件卡也可以直接生成
.\daydreamer.cmd "C:\Users\Eldon\Downloads\Bold Maker\outputs\daydreamer-prd\生活事件卡_骑自行车02.json"
```

未指定制作参数时默认 **12秒、16:9、720P**，事件卡中的制作参数优先。运行采用 Qwen 创作和 Wan 视频模型，后段使用前段实际剪辑末帧续接。每次视频提交超过 **5分钟**仍未完成，会确认状态后最多自动重提一次。请保持运行窗口打开。

新创作任务会生成随机种子，并在 Qwen 各阶段请求中明确传入 `seed`。依次生成主题、连续行动脚本、画风、分镜规划，再逐镜生成镜头内容。程序原样保留已确定的主题、脚本和画风，组装最终故事包，不再让模型重写整包JSON。取消独立故事复核；不增加生成后的内容核查、评分或模型改写。已有本地格式、引用、时长和文件校验保留。已取消多候选、随机挑选和历史相似度重写。恢复任务沿用已保存的种子和结果。

分镜规划只生成镜头与beat的对应、时长、动作范围和声音等新增信息；每次镜头请求仅输出一个镜头。后镜开始状态由程序复制前镜结束状态，编号、来源引用和固定字段也由程序补齐。分镜输出使用独立JSON Schema，规划输出上限8192 token、单镜输出上限4096 token；错误修正只附带当前规划或镜头。记录保存在 `story/seeded/assembly-v1/`（重开时位于对应restart目录）。

故事以一个持续行动目标展开，如街头追逐、外星交战或机关穿越。先写应对与后果，再按动作边界分配镜头与时长；不安排反转、不强制起承转合，可以在整场事件尚未结束时停在自然动作节点。非写实风格保留清楚的体积、支撑、道路和遮挡关系。每段视频提示词携带当前目标、前段进展和对应脚本，避免只有画面续接而没有剧情推进。新规则用于新建任务；已有成片与历史记录不重写。

若某个创作阶段的一次格式修正仍未通过，会自动换新 seed，从主题开始重新创作。已提取的事件卡复用，任务编号不变，失败响应保留。默认最多自动重开2次（总计3轮创作），由 `[story].max_creation_restarts` 控制，可设0–5；上限用尽后停止，不提交视频。恢复不会重置次数。网络超时、鉴权失败或素材需要补充等情况不会触发重开。

创作种子保存在 `data/runs/<任务编号>/input/creativity.json`，新阶段响应和实际请求种子保存在 `story/seeded/responses/`，输出目录附带 `创作随机种子.json`。旧候选任务恢复时复用已选定的阶段，不再继续抽选候选。详见 [随机创作说明](docs/creativity.md)。

音乐和音效服务尚未接入，因此默认导出无声预览。输出位于 `outputs/<生成任务编号>/`，包含视频、完整故事与分镜 JSON、可读分镜和运行记录。从视频启动时，还会一并保存 `生活事件卡.json` 和 `素材提取关联.json`。原始片段与检查点保存在 `data/runs/<生成任务编号>/`，提取记录保存在 `data/extractions/<全流程任务编号>/`。

中断或本次等待结束后，使用屏幕显示的任务编号恢复，复用已完成内容：

```powershell
.\daydreamer.cmd -Run 任务编号
```

原始视频入口从开始到结束使用同一个全流程任务编号恢复，自动找到已关联的生成任务；已完成的理解、故事和视频片段会复用。重新选择视频或事件卡会创建新任务。理解无有效事件、多事件冲突、故事需要补充材料、视频明确失败或提交结果不明时，流程会停止并保留记录。自动重提次数用尽后也会停止。恢复不会重置重提次数。

全流程入口代表用户已选择本次素材进行创作，因此有效事件卡校验通过后直接进入生成。卡片仍如实保留“未做历史去重”的字段；此次生成意图单独记录，不伪造去重结果。

需要自定参数或接入本地音频清单时，可用完整入口：

```powershell
.\.venv\Scripts\python.exe daydreamer.py run --video "生活视频.mp4" --duration 12 --ratio 16:9 --resolution 720P

.\.venv\Scripts\python.exe daydreamer.py run --events "事件卡.json" --duration 12 --ratio 16:9 --resolution 720P --audio "音频清单.json"
```

`run` 的视频阶段本次最长等待默认3600秒，可用 `--wait` 调整；它与每段5分钟的重提阈值独立。密钥 CSV 路径配置在 `config/default.toml` 的 `credentials.csv_path`，密钥本身不写入配置；也支持原有环境变量和 `.env`。

Qwen 故事请求默认等待上限为600秒，可通过 `[story].request_timeout_seconds` 调整（30–1800秒）。它与视频生成的5分钟重提规则独立；旧任务未保存该参数时也使用600秒。超时后不会自动重复提交故事请求，可用原任务编号恢复，已完成的事件卡和创作阶段会复用。创作阶段网络错误记录在 `story/seeded/errors/`，包含阶段、种子、耗时及错误原因。

## 可选：只从视频提取生活事件卡

运行这一条命令，弹窗选择本地视频：

```powershell
& "C:\Users\Eldon\Downloads\Bold Maker\daydreamer-agent\daydreamer.cmd" -Extract
```

也可在项目目录直接传入视频路径：

```powershell
.\daydreamer.cmd -Extract "C:\素材目录\生活视频.mp4"
```

程序使用 `qwen3.8-max` 实际理解视频画面，通过严格 JSON Schema 输出七项生活事件卡。完成后只保存事件卡，不启动幻想故事或视频生成。

- 不分析声音，`audio_info` 固定为空数组；传给模型的处理副本没有音轨，原视频不修改。
- 保留 `key_frames` 的文字描述、参考说明及原视频秒数位置；`image_uri` 固定为 `null`，不提取或保存图片。
- ID由程序提供，实际发生时间未知则为 `null`。不推测人物心理、动机或未拍到的经过。
- 未做去重与记忆关联，去重字段为 `null`，记忆数组为空，筛选状态为 `waiting_accumulation`。生成任务状态另行记录。
- 首版输入为围绕同一事件的视频；多个明显独立事件会停止并说明原因。全黑或没有可辨认事件时不导出空卡片。
- 默认支持2秒至30分钟视频，超过60秒分段理解，汇总时换算回原视频时间。分段长度、最大时长与抽帧频率可在配置的 `[extraction]` 中调整。模型估计的时间位置会做范围校验，不代表已逐帧核对。

输出在 `outputs/event-cards/<任务编号>/`：事件卡 JSON、素材引用表和提取记录。事件卡是唯一符合七项事件格式的文件，后续创作时请选择以 `event_` 开头的 JSON；两份记录不是事件卡。原始视频通过路径和指纹关联，移动后未完成任务需要将素材放回原位。

恢复提取任务：

```powershell
.\daydreamer.cmd -Extract -Run 任务编号
```

已完成的分段和模型响应会复用；格式或引用错误最多修正一次，次数不会因恢复而重置。请求超时后显式恢复可能重新调用未收到结果的文本接口；Wan视频生成的5分钟自动重提规则不用于本入口。已完成任务可重新导出，无需再次调用模型。

提示词在 [prompts/event_extraction.md](prompts/event_extraction.md)，格式约束在 [schemas/life_event_card.schema.json](schemas/life_event_card.schema.json)。修改只影响新建任务，已有任务使用创建时保存的规则。原格式参考保存在 `resources/event-card/`。

技术实现和边界见 [事件卡提取说明](docs/event-extraction.md)。

## 当前进度

第一版命令行流程已实现：事件卡读取、Qwen 分阶段创作、提示词组装、WanVideo 异步任务与恢复、本地音频接入、视频拼接和归档。音乐与音效的自动生成服务仍待确定。虚拟环境位于本目录的 `.venv`，不共享系统第三方包。

- 故事模型：`qwen3.8-max`
- 视频模型：首段 `wan3.0-video-prime`，后段 `wan3.0-video-prime`
- 新模型统一支持文生视频与首帧续接，原生音轨与自动提示词改写关闭。原有命令无需修改；历史任务保留原模型快照，重做旧故事请用 `import-story` 创建新任务。
- 暂不做媒体内容检查。
- `run` 一键完成全流程；也可用 `plan` 只生成故事和请求，查看分镜后用 `render` 启动视频生成。
- `demo` 完全离线；生成的工程测试画面不是模型创作样片。
- 新任务默认 `frame_chain`：按顺序生成，每段实际剪辑末帧作为下一段首帧。可用 `--continuity single_take` 改为3–15秒单次文生视频。续接实现与能力边界见 [连续性要求](docs/continuity.md)。

详细设计见 [架构与文件结构](docs/architecture.md)。

## 文件结构

```text
daydreamer-agent/
├── .venv/                     # 本地 Python 虚拟环境，不进入版本管理
├── .env.example               # 密钥与端点配置说明，密钥为空
├── .gitignore
├── pyproject.toml             # Python 项目元信息，无第三方 Python 依赖
├── daydreamer.py              # 命令行入口
├── daydreamer.cmd             # Windows 一键入口，选原始视频或JSON事件卡
├── daydreamer.ps1             # 本地虚拟环境与文件选择窗口
├── .tools/                   # 本地 FFmpeg/FFprobe，不进入版本管理
├── examples/                 # 虚构事件卡、故事包和音频清单示例
├── README.md
├── config/
│   └── default.toml           # 模型选择与流程默认配置
├── docs/
│   └── architecture.md
├── resources/
│   └── daydreamer-style-transfer/ # 本次采用的技能快照
├── src/
│   └── daydreamer_agent/
│       ├── domain/            # 输入输出契约、镜头与任务状态
│       ├── application/       # 流程编排、恢复与重试
│       ├── story/             # 技能装载、主题/脚本/风格/分镜
│       ├── prompts/           # 视频提示词模板与组装
│       ├── providers/         # Qwen、WanVideo、音频接口适配
│       ├── media/             # 时间线、剪辑、混音、文件技术检查
│       ├── memory/            # 记忆读取与增量草案
│       └── storage/           # 本地文件与 SQLite 任务记录
├── inputs/
│   └── event_cards/           # 原始事件卡 JSON
├── data/
│   ├── memory/               # 世界记忆与版本
│   └── runs/                 # 每次任务的中间产物与成片
├── outputs/                  # 一键运行的视频与可读故事导出
└── tests/                    # 后续业务测试
```

模块职责已在架构文档中定义；模型平台认证封装在 providers 层。

## 虚拟环境

在此目录使用 PowerShell：

```powershell
.\.venv\Scripts\Activate.ps1
python --version
```

也可直接使用虚拟环境解释器，无需激活：

```powershell
.\.venv\Scripts\python.exe --version
```

当前使用 Python 标准库，无需安装第三方 Python 依赖。媒体处理需要 FFmpeg 和 FFprobe；本机已安装在项目 `.tools` 中。换机器后可设置 `FFMPEG_PATH`、`FFPROBE_PATH`，或将工具放入 `.tools`。当前安装来源及校验见 [依赖记录](docs/dependencies.md)。

## 运行方式

以下命令在项目目录执行。密钥 CSV 只读取到内存，不复制到项目；该文件路径仅用于本机，换机器可以使用 `.env` 或环境变量。

```powershell
# 检查配置与媒体工具，不调用网络
.\.venv\Scripts\python.exe daydreamer.py doctor

# 离线演示故事与提示词
.\.venv\Scripts\python.exe daydreamer.py demo

# 用测试图样、测试音调验证拼接与混音，不调用模型
.\.venv\Scripts\python.exe daydreamer.py demo --with-media

# 调用 Qwen 创作；示例卡是虚构测试数据
.\.venv\Scripts\python.exe daydreamer.py --credentials-csv '..\credentials.csv' plan --events examples/events.json

# 查看任务，或追加 --run 任务编号查看单任务
.\.venv\Scripts\python.exe daydreamer.py status
```

`plan` 返回任务编号。故事位于 `data/runs/<任务编号>/story/result.json`，每镜完整请求位于 `shots/<镜头编号>/video_request.json`。模型创作按主题、连续脚本、风格、分镜四个阶段执行，无独立文本复核，成功阶段不会在恢复时重复调用。

```powershell
# 恢复中断的故事生成
.\.venv\Scripts\python.exe daydreamer.py --credentials-csv '..\credentials.csv' plan --run <任务编号>

# 正式提交视频任务，此命令会使用视频模型配额或产生费用
.\.venv\Scripts\python.exe daydreamer.py --credentials-csv '..\credentials.csv' render --run <任务编号>

# 为明确失败的单镜准备新尝试；之后再执行 render
.\.venv\Scripts\python.exe daydreamer.py retry-shot --run <任务编号> --shot shot-01

# 重做已完成的镜头；续接模式同时使后续片段失效，之后再执行 render
.\.venv\Scripts\python.exe daydreamer.py regenerate-shot --run <任务编号> --shot shot-01

# 提交结果未知时，在平台核对后补录原任务编号；不会盲目重发
.\.venv\Scripts\python.exe daydreamer.py attach-task --run <任务编号> --shot shot-01 --task-id <平台任务编号>

# 镜头全部完成后生成无声预览
.\.venv\Scripts\python.exe daydreamer.py compose --run <任务编号> --preview

# 指定背景音乐和音效清单，生成正式成片
.\.venv\Scripts\python.exe daydreamer.py compose --run <任务编号> --audio <音频清单路径>
```

`render` 默认最多运行30分钟，按约15秒间隔轮询；`--wait 0` 可执行一次状态检查，`--wait` 的范围为0–3600秒。命令退出后进度保留，再次运行会继续当前尝试。命令未运行时不会后台计时执行操作，但恢复时会根据已保存的提交时间判断是否超时。

每段提交成功后，默认等待5分钟；仍为PENDING/RUNNING时再查询一次，未完成则自动重提该段，最多重提1次。新尝试使用同一提示词与前段末帧，后段只依赖新尝试。新尝试再次超时后进入needs_resolution，保留其任务编号，后续仍可用render查询是否最终完成，不再自动新增尝试。每段分别计算重提次数。

可在`config/default.toml`的`[video]`调整`generation_timeout_seconds = 300`与`max_timeout_resubmissions = 1`，重提次数设为0即关闭（允许0–5）。配置随新任务保存快照；旧任务若缺少这些配置使用上述默认值，若缺少提交时间则从首次恢复观察开始计时。提交结果未知、网络查询失败、明确FAILED/CANCELED或下载失败不触发此规则。

旧远端任务保留记录，不自动取消，仍可能完成和计费；只采用新尝试参与拼接。自动重提不会重新调用故事模型。视频和音频下载、处理失败也可以恢复。远端任务过期或提交状态未知时，需核对平台记录。

事件卡制作参数可以用 `--duration 15 --ratio 9:16 --resolution 720P` 覆盖。视频生成前所有镜头必须有 3–15 秒整数时长、画幅和分辨率；未确定时仍可创作故事。`--memory` 接收世界记忆 JSON；也可用 `--world-id` 读取 `data/memory/<world_id>/current.json`。

输入支持简化事件卡、事件卡数组、包含 `event_cards` 的对象，以及包含 `basic_info`、`event_description`、`key_frames`、`audio_info` 等字段的完整单张事件卡。系统在内存中规范化结构，保留原始文件、动作顺序与素材引用。本版只将事件卡文字发送给故事模型，不读取引用的视频、图片或原始音轨。

修改故事请保留原任务，导入为新任务：

```powershell
.\.venv\Scripts\python.exe daydreamer.py import-story --events examples/events.json --story <修改后的故事JSON>
```

导入故事执行结构、引用与时长校验；语义复核由导入者负责。不要直接改已有任务的故事或视频请求文件，系统会阻止其与旧镜头混用。

## 音频与输出

[音频清单示例](examples/audio-manifest.json) 中的路径相对清单文件所在目录。背景音乐会循环铺满成片，音效按 `at_seconds` 放入时间线，`gain` 控制音量。所有原视频音轨均丢弃，避免保留模型生成的人声。清单要求声明 `contains_speech: false`；系统不做自动人声识别，声明不代表已验证。

最终视频为 `data/runs/<任务编号>/final/video.mp4`；`manifest.json` 记录采用的镜头、音频、校验值和规则版本。缺少音频时保持 `awaiting_audio`。世界记忆增量只保存草案，不自动提交。第一版画面衔接采用顺序硬切，渐变、交叉淡化等剪辑转场暂未实现。

## 验证

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

测试不调用真实模型。覆盖输入和引用检查、提示词信息完整性、失败恢复、重复提交防护、密钥隔离，以及本地媒体合成。没有 FFmpeg 的机器会跳过媒体集成测试。

## 配置与密钥

`config/default.toml` 保存非敏感配置；`.env.example` 说明所需环境变量。制作参数优先级为命令行 > 事件输入 > 项目默认值；认证优先级为环境变量 > 显式指定的 CSV > 项目 `.env`。不在状态输出或任务档案中保存认证数据。

API Key 不写进代码、提示词、运行日志或任务归档。用户提供的原始密钥 CSV 保持原位置，本项目未复制其内容。不要将该 CSV 提交到版本库。
