# 白日梦想家 · Daydreamer

> **让平凡的一天，有个白日梦版本。**

心跳快的那一刻，相机自动留下画面与声音——你继续经历这一刻。
这些片段会被整理出动作、声音与细节，生成以你为主角的幻想故事：**人不动、构图不动，只换它所在的世界。**

`BOLD MAKER 2026` 影石 Insta360 `</智能影像>` 挑战赛 · **AI + 影像内容创作** · 第 27 组 **FuntechLab**

---

## 一、它做什么

| 步骤 | 发生了什么 |
| :--- | :--- |
| **1. 心动一刻** | 连接心率监测设备（BLE 心率带 / 手表广播）。心率变化满足触发条件时，相机**自动**留下当下的画面与声音——不用按快门，佩戴者继续经历这一刻。 |
| **2. 进入白日梦** | 系统从片段中整理出动作、声音与细节，生成以佩戴者为主角的幻想故事：现实里我握着车把骑过熟悉的街道；白日梦里街道亮起霓虹，我追着一盏红色尾灯驶入陌生的夜城。 |
| **3. 另一个我** | 记住你的角色、记住故事里去过的地方，让新的生活片段接续新的冒险。打开 App，看看另一个我，今天经历了什么。 |

**三个不变量**：只重绘关键帧，不改姿态、不改朝向、不改画面比例——**只换皮，不动人**。

---

## 二、仓库结构

本仓库是赛事交付代码，含**三端 + 部署 + 文档**：

| 目录 | 是什么 | 技术栈 |
| :--- | :--- | :--- |
| [`heartbeat-camera/`](heartbeat-camera/) | **Android 采集端**。订阅 BLE 心率 → 迟滞阈值引擎 → 调 Insta360 SDK 控制 GO Ultra 开停拍；含素材上传链路 | Kotlin / Android SDK 35 / Insta360 SDK 2.1.5 |
| [`daydreamer-agent/`](daydreamer-agent/) | **服务端创作 Agent**。生活视频或事件卡 → 事件理解 → Qwen 分阶段创作故事与分镜 → Wan 逐段生成视频 → 拼接归档 | Python 3（**仅标准库**）/ FFmpeg |
| [`web/`](web/) | **网页交互端**。移动端上传与故事查看界面 | 原生 JS / Node |
| [`deploy/`](deploy/) | 服务器部署脚本（ECS 初始化、证书、探针、远程执行） | Shell / Python |
| [`docs/`](docs/) | 方案架构、APK 构建交接说明、打包说明、设计稿 | Markdown / HTML |

```text
FuntechLab/
├── heartbeat-camera/          # ① Android 采集端
│   ├── app/src/main/java/com/insta360/heartbeat/
│   │   ├── ble/               #   扫描 + 订阅 0x2A37 心率
│   │   ├── threshold/         #   迟滞阈值引擎（防阈值附近抖动）
│   │   ├── camera/            #   连接/激活/开停拍
│   │   ├── upload/            #   .lrv → .mp4 换壳 + multipart 上传
│   │   └── ui/                #   界面 + 编排
│   ├── server/upload_server.py
│   └── docs/                  #   构建踩坑、BLE 排查、ANR、闪退等一线记录
├── daydreamer-agent/          # ② 服务端创作 Agent
│   ├── src/daydreamer_agent/
│   │   ├── event_extraction/  #   视频 → 生活事件卡
│   │   ├── story/             #   主题/脚本/画风/分镜 四阶段创作
│   │   ├── prompts/           #   视频提示词组装
│   │   ├── providers/         #   Qwen / Wan / 音频适配
│   │   └── media/             #   时间线、剪辑、混音
│   ├── deploy/                #   systemd + nginx
│   └── docs/                  #   架构、连续性、事件提取、验证
├── web/                       # ③ 网页交互端
├── deploy/                    # 服务器部署脚本
└── docs/                      # 方案架构 / 构建交接 / 打包说明 / 设计稿
```

---

## 三、快速开始

### ① Android 采集端

需要：Android Studio（JDK 17）、Android SDK 35、**一台 Android 真机**（影石 SDK 只有 arm64 原生库，模拟器跑不了）、一台 Insta360 GO Ultra。

```bash
cd heartbeat-camera
cp gradle.properties.example gradle.properties   # 填入你自己的 Insta360 Maven 凭据
./gradlew assembleDebug                          # APK → app/build/outputs/apk/debug/
```

> ⚠️ **拉取 Insta360 SDK 依赖需要一对 Maven 凭据**，`settings.gradle.kts` 访问的是需要认证的私有仓库。
> `gradle.properties` 已在 `.gitignore` 中，**真实凭据不入库**；也可以写进 `~/.gradle/gradle.properties`。
> 凭据来自影石官方 SDK demo 工程，或向影石 SDK 对接同学索取。

相机激活需要自备 `AppId + SecretKey`（在 [Insta360 开发者平台](https://insta360.com/developer/tutorial?type=sdk) 申请）。

**无需相机也能跑通链路**：App 内置「模拟心率」源，可直接验证 `心率 → 阈值 → 触发` 全流程。

端到端上传测试（不联网、不用云服务器）：

```bash
cd heartbeat-camera/server
python upload_server.py --port 8000 --out ./uploads
# 另开终端：adb reverse tcp:8000 tcp:8000
# App 里上传地址保持 http://127.0.0.1:8000/upload → 录一段 → 点上传
```

### ② 服务端创作 Agent

需要：Python 3、FFmpeg + FFprobe、DashScope（阿里云百炼）API Key。

```bash
cd daydreamer-agent
cp .env.example .env        # 填入 DASHSCOPE_API_KEY 等
python daydreamer.py doctor # 检查配置与媒体工具，不调用网络
python daydreamer.py demo   # 完全离线的故事与提示词演示

# 从视频跑全流程：理解视频 → 事件卡 → 创作 → 逐段生成视频 → 拼接
python daydreamer.py run --video "生活视频.mp4" --duration 12 --ratio 16:9 --resolution 720P

# 只提取生活事件卡
python daydreamer.py -Extract "生活视频.mp4"
```

> ⚠️ **`render` / `run` 的视频阶段会消耗 Wan 视频模型配额并产生费用。** 先用 `demo` 与 `plan` 验证。
> API Key 不写进代码、提示词、日志或任务归档；密钥文件只在内存中读取，不入库。

### ③ 网页交互端

```bash
cd web
node server.cjs
```

---

## 四、关键设计

**为什么是「心跳」而不是「画面」**

影石的 AI 导演回答的是「哪一段**最好看**」；我们回答的是「哪一刻**他真的在**」。
心率过阈值的时刻大多不高光——爬楼、赶车、被吓一跳——而这恰恰是命题本身：
**剪出来的是作品，不是记忆。**

**判据只有一个变量**

```text
心率源(BLE 心率带/手表) --BLE--> 手机 App: 迟滞阈值引擎 --WiFi--> GO Ultra 录像 / 间隔拍照
```

其余全部复用影石已有能力：整机拍摄由固件负责、App 只做事后同步、健康数据接入、AI 重绘。

**只新增后两层**

| 层 | 内容 | 状态 |
| :--- | :--- | :--- |
| L1 采集 | 固件定时录像 + 素材回传 | 复用已有 |
| L2 理解 | 抽帧去重 + ASR →「一刻 = 一张画面 + 当时那句话」 | 复用已有（单段 ~100MB → ~1MB） |
| **L3 生成** | 预设风格模板 → 关键帧重绘 | **本次新增** |
| **L4 输出** | 双轨成片（现实轨 / 白日梦轨） | **本次新增** |

L3 的三个硬决定：① 只重绘关键帧，**不生成整段视频**；② 走「关键帧 + 角色参考图 → 图生图」，**不走纯文生视频**（控不住脸）；③ `face_id` 是生成层的必需输入。

---

## 五、我们明确不做什么

- 不重做「怎么拍」「怎么剪」——影石已经答完了，我们接在**成片之后**。
- 不做实时提醒 / 场景描述（需要极高准确率，错一次就是伤害）。
- 不做认知评估 / 衰退推断（属医疗诊断，需资质与临床验证）。
- 不做跌倒 / 走失预警（另一类产品，另一套责任）。
- 不做家门外行踪追踪、不做夜间与私密空间采集。

---

## 六、部署说明

`deploy/` 与 `daydreamer-agent/deploy/` 中的脚本、`nginx.conf` 含服务器配置模板。
**仓库中的服务器地址已统一替换为占位符 `your-server.example.com`**，部署时请替换为你自己的域名或 IP，并自行签发证书：

```bash
# 证书路径示例（nginx.conf 中同步替换）
/etc/letsencrypt/live/your-server.example.com/fullchain.pem
```

---

## 七、许可与声明

- 本仓库代码为 **FuntechLab 团队原创**，仅用于 `BOLD MAKER 2026` 赛事提交与评审。
- 依赖的 Insta360 SDK、DashScope（Qwen / Wan）模型服务版权归各自权利人所有，使用需遵守其服务条款。
- 仓库内不含任何 API Key、Maven 凭据或真实服务器地址；示例配置见各模块的 `.env.example` 与 `gradle.properties.example`。
- 文档中提到的人物影像素材、示例事件卡均为测试数据或虚构样例。

---

<p align="center">
<b>心动一刻 · 白日梦想家</b><br />
献给每一位生活中的「普通人」
</p>
