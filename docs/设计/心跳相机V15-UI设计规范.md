# 心跳控制相机 · V15 UI 设计规范

> 交付日期：2026-09-23 ｜ 设计基线：V14 功能集 ｜ 配套原型：`心跳相机V15-界面原型.html`
> 适用机型：Insta360 GO 3S / GO Ultra（SDK 2.1.5），Android minSdk 29 · targetSdk 35

---

## 一、设计目标

V14 的功能已经完备，问题全部出在**界面的组织方式**上。本次重构围绕三个目标：

| 目标 | 落实手段 |
|---|---|
| **运行时数据不丢失** | 心率、连接状态、录制状态常驻，不与滚动位置耦合 |
| **流程状态外显** | 用 stepper / 状态胶囊 / 会话列表替代说明书文案 |
| **机制可被理解** | 把迟滞阈值从「要记住的规则」变成「看得见的区间带」 |

### 三条设计原则

1. **先任务，后控件** —— 分区依据是「用户此刻要完成什么」，不是「这是什么类型的控件」。所以「连接相机」与「心率源扫描」不再相邻 —— 它们属于不同任务阶段。
2. **一屏一焦点** —— 每个 Tab 只有一个视觉重心：控制台是心率环，素材是列表，设置是滑块，诊断是自检卡。
3. **状态用三重编码** —— 颜色 + 图标 + 文字并存。任何只靠颜色传达的信息，都视为缺陷（WCAG 1.4.1）。

---

## 二、信息架构

```
心跳控制相机
├─ ① 控制台  Console          ← 默认落地页，演示时全程停留在此
│   ├─ 心率环仪表（60sp 数值 + 区间游标）
│   ├─ 触发区间带（安全区 / 迟滞带 / 触发区）
│   ├─ 设备卡（相机 + 心率源）
│   └─ 主开关（启动心率触发 ↔ 停止触发并结束录制）
├─ ② 素材    Media
│   ├─ 分段：相机素材 / 服务器素材
│   ├─ 三步 stepper：读取列表 → 选中一条 → 上传
│   ├─ 单选素材列表（视频优先，时间倒序）
│   └─ 底部固定操作条（已选摘要 + 上传/取消 + 进度）
├─ ③ 设置    Settings
│   ├─ 拍摄模式（录像 / 间隔拍照）
│   ├─ 心率触发阈值（双滑块 + 区间预览 + 自动钳制）
│   ├─ 间隔（步进器 + 快捷 chip）
│   ├─ 折叠区：相机激活 / 服务器地址  ← 一次性配置
│   └─ 连接管理：停止/断开（危险）
└─ ④ 诊断    Diagnostics
    ├─ 连接自检（权限 / 相机 / 心率源 / 服务器，可单项重测）
    ├─ 运行日志（分级着色终端）
    └─ 复制 / 清空
```

**迁移动线**：V14 的「日志区」从页顶第 5 个区块移到独立 Tab —— 它是排障信息，正常使用时不应占据首屏。

---

## 三、设计 Token

### 3.1 色彩

深色为默认主题。选择理由不是审美偏好：相机类 App 在户外强光下取景，深色界面减少屏幕自身反射对取景的干扰；同时深色下心率红（`#FF4D5E`）的视觉权重显著高于浅底。

| 语义 | Token | 深色 | 浅色 | 对比度（正文基准） |
|---|---|---|---|---|
| 基底 | `--bg-base` | `#0A0C10` | `#F4F5F7` | — |
| 卡片 | `--bg-surface` | `#12151B` | `#FFFFFF` | — |
| 内嵌层 | `--bg-surface-2` | `#1A1E26` | `#F0F2F5` | — |
| 轨道/缩略 | `--bg-surface-3` | `#232833` | `#E6E9EE` | — |
| 描边 | `--border` | `#252A34` | `#E4E7EC` | — |
| 主文字 | `--text-primary` | `#F2F4F7` | `#101828` | 15.8:1 / 17.4:1 · AAA |
| 次文字 | `--text-secondary` | `#9AA3B2` | `#5A6473` | 7.4:1 / 6.2:1 · AAA·AA |
| 三级文字 | `--text-tertiary` | `#7A8494` | `#6E7787` | 4.6:1 · AA |
| 心率 | `--hr` | `#FF4D5E` | `#E5484D` | 5.9:1 / 4.6:1 · AA |
| 主操作 | `--accent-solid` | `#2563EB` | `#2563EB` | 白字 5.2:1 · AA |
| 高亮/选中 | `--accent` | `#3D8BFF` | `#2563EB` | 6.4:1 · AA |
| 成功 | `--ok` | `#22C55E` | `#16A34A` | 8.3:1 · AAA |
| 警告 | `--warn` | `#F59E0B` | `#D97706` | 8.6:1 · AAA |
| 危险 | `--danger` | `#EF4444` | `#DC2626` | 4.6:1 · AA |
| 危险文字 | `--danger-text` | `#FF8A8A` | `#B91C1C` | 深底专用 |

**语义约束（重要）**：心率红**只**用于心率数值与录制态，**不**兼作危险色。界面中唯一的危险色只出现在「停止 / 断开」。混用会让用户无法判断「红色是心跳还是出错了」。

### 3.2 字阶（1.25 比例）

| 层级 | 字号/行高 | 字重 | 用途 |
|---|---|---|---|
| Display | 60 / 64 | 700 | 心率数值（`letter-spacing: -0.045em`，`tabular-nums`） |
| H1 | 20 / 26 | 700 | Tab 页标题 |
| H2 | 16 / 22 | 600 | 卡片标题、区块名 |
| Body | 14 / 21 | 400 | 正文、列表主文本 |
| Label | 12 / 17 | 600 | 文件名、次要标签、胶囊 |
| Caption | 11 / 16 | 400 | 说明文字、时间戳 |

数字必须启用 `font-variant-numeric: tabular-nums` —— 心率 88→89 时等宽数字不抖动，否则整块会反复重排。

字体栈：`-apple-system, "PingFang SC", "HarmonyOS Sans SC", "Segoe UI", "Microsoft YaHei", sans-serif`

### 3.3 间距 · 4pt 网格

`4 / 8 / 12 / 16 / 20 / 24 / 32`
- `4` 图标与文字内距
- `8` 行内元素间隙、列表行间距
- `12` 卡片之间
- `16` 页面左右边距（全站统一，不出现 20dp 与 16dp 混用）
- `24` 区块之间

### 3.4 圆角

`8` 输入 / 徽章 ｜ `12` 按钮 / 列表行 / 终端 ｜ `16` 卡片 ｜ `24` 底部弹层 ｜ `999` 胶囊
> 规则：圆角约为组件高度的 1/4，与内边距保持 1:2 递进，避免「大圆角 + 小内距」的松散感。

### 3.5 动效

| 场景 | 时长 | 曲线 |
|---|---|---|
| 状态色切换、图标旋转 | 150ms | `cubic-bezier(.2,.8,.2,1)` |
| 页面切换、列表展开 | 250ms | 同上 |
| 心率环进度、上传进度 | 250ms | 线性（跟随数据） |
| 录制脉冲 | 1400ms 循环 | ease-in-out |

必须响应系统「移除动画」开关（Android `Settings.Global.ANIMATOR_DURATION_SCALE`），关闭时降级为瞬时切换。

---

## 四、页面规格

### ① 控制台

| 元素 | 规格 |
|---|---|
| AppBar | 52dp 高；标题 16sp/700；右侧状态胶囊（相机 + 心率），26dp 高 |
| 心率环 | 212×212dp；环宽 13dp；起角 150°、扫角 240°；轨道 `--bg-surface-3` |
| 环进度映射 | 40~200 bpm 线性映射到 0~100% 弧长（比 30~250 映射更灵敏，日常区间变化可见） |
| 心率数值 | 60sp/700，色值随状态变化：静息 `--hr`，触发中 `--hr` + 状态副文案 |
| 区间带 | 300×44dp SVG/自绘；三段：安全区（ok 50%）· 迟滞带（warn 90%）· 触发区（danger 34%） |
| 游标 | 2dp 竖线 + 4dp 圆点，位置 = `(hr − 30) / 220` |
| 主开关 | 56dp 高，圆角 16dp；待机 = accent 实底「启动心率触发」；触发中 = danger 弱底「停止触发并结束录制」 |
| 会话列表 | 每行 ≥ 52dp；REC 行用 `--hr-soft` 底 + `--hr` 描边 |

**关键状态分支**
1. 未连接相机 → 主开关置灰（`opacity .42`），下方 hint 提示「先连接相机」并给出跳转
2. 已连接、未启动 → 主开关 accent 实底
3. 触发中 → AppBar 出现录制胶囊（脉冲点 + 计时），主开关转危险态，会话列表出现 REC 行
4. 相机断开但心率仍在 → 状态胶囊转灰，会话列表提示「已缓存，未上传」

> **为什么会话列表必须存在**：`isWorking()` 在间隔拍照/延时的两次快门之间返回 `false`，界面无法用它判断「在不在拍」。V15 让界面只显示 App 自己的 `armed` 标志，并用会话列表给出「拍了几段、每段多久」的事实依据，消除黑盒感。

### ② 素材

| 元素 | 规格 |
|---|---|
| 分段控件 | 38dp 高，选中项底 `--bg-surface-3` |
| Stepper | 三步等宽；连接线 2dp；完成态 `--ok`，当前步 `--accent` + 4dp 光晕 |
| 列表行 | ≥ 52dp；缩略图 44×44dp 圆角 9dp；选中态 `--accent-soft` 底 + `--accent` 描边 + 实心 radio |
| 类型徽章 | 11sp 内联：LRV 用 warn、视频用 accent、照片用 ok |
| 操作条 | 固定于 TabBar 上方；上行显示「已选 N 条 · 原始大小 → 预计 MP4 大小」 |
| 进度 | 6dp 高，圆角 3dp；上传中显示百分比与速率，可取消 |

**流程守卫（必须在界面上体现）**
- 未连相机 → 「读取列表」可点，但结果为空时提示「需连接相机 WiFi」
- 列表为空 → 第二步结点保持灰，不出现「可上传」假象
- 下载中 → 显示进度；上传阶段网络切换（相机热点无公网）→ 明确提示「已缓存，可在有网时继续」，**不要让用户以为要重传**
- 切换选中项 → 若缓存属于上一条素材，必须重新下载（不可复用，否则传错文件）

### ③ 设置

| 元素 | 规格 |
|---|---|
| 分段控件 | 同素材页；切换模式后**间隔项按需显隐**（录像模式下间隔置灰并说明原因） |
| 滑块 | 轨道 6dp；填充与滑块描边随语义（开拍 = `--hr`，停止 = `--ok`） |
| 数值 | 滑块行右侧 17sp/700，可直接点击进入精确输入 |
| 联动钳制 | 停止阈值拖动越界时，实时压回「开拍 − 1」，**并在滑块下方即时显示一行提示**，不静默改数 |
| 踏破提示 | 开拍/停止阈值间距 < 3 bpm 时，出现 warn 提示「迟滞带过窄，可能在阈值附近反复开关」 |
| 间隔 | 步进器 40dp 按钮 + 中间大数值；下方 6 个快捷 chip |
| 折叠项 | 56dp 行高，右侧 chevron 旋转 90° 表示展开 |
| 危险按钮 | `--danger-soft` 底 + `--danger` 描边 + `--danger-text` 字，**仅在设置页末出现一次** |

### ④ 诊断

| 元素 | 规格 |
|---|---|
| 自检行 | 48dp；左侧 22dp 圆形状态图标；右侧「重测」按钮（36dp） |
| 终端 | 背景 `#05070A`（浅色主题下仍保持深色，以保证日志可读）；字号 10.5sp；行高 1.85 |
| 日志级别色 | 时间 `#4B5563` · 信息 `#58A6FF` · 成功 `#3FB950` · 警告 `#D29922` · 错误 `#F85149` · 高亮 `#E6EDF3` |
| 默认折叠 | 仅渲染最近 200 行，避免长日志导致的渲染卡顿 |
| 操作 | 复制（含全量）/ 清空 |

---

## 五、组件状态矩阵

| 组件 | Default | Hover/Press | Focus | Disabled | 特殊态 |
|---|---|---|---|---|---|
| 主按钮 | accent 实底 | `brightness(1.1)` | 2dp accent 外描边 + 2dp 偏移 | opacity .42 | 录制态转 danger |
| 次按钮 | surface-2 + border | 描边转 accent | 同上 | opacity .42 | — |
| 危险按钮 | danger-soft + 描边 | 底色加深至 20% | 同上 | opacity .42 | — |
| 输入框 | surface-2 | — | 描边转 accent | opacity .5 | 校验失败：danger 描边 + 下方 11sp 说明 |
| 列表行 | surface-2 | 底色 → surface-3 | accent 描边 | opacity .5 | 选中：accent-soft + 实心 radio |
| 状态胶囊 | surface-2 | — | — | — | ok / hr / warn / busy 四套配色，均带图标 |
| 分段项 | 透明 | — | — | opacity .5 | 选中：surface-3 + 阴影 |

---

## 六、无障碍清单（交付前逐项验收）

- [x] 所有正文文本对比度 ≥ 4.5:1，大字 ≥ 3:1
- [x] 所有可点击元素 ≥ 48×48dp（主操作 56dp）；相邻目标间距 ≥ 8dp
- [x] 状态三重编码：颜色 + 图标 + 文字（色盲可辨）
- [x] 图标按钮提供 `contentDescription`（如「停止触发并结束录制」）
- [x] 滑块提供 `accessibilityValue`（当前值 + 范围），支持 TalkBack 手势调节
- [x] 支持系统字体缩放至 200%：容器用 `minHeight` 而非固定 `height`，文本用 `wrap_content`
- [x] 尊重「移除动画」系统开关
- [x] 触摸目标不依赖 hover；纯触屏下所有交互均可达
- [x] 心率变化不做仅靠动画的通知（避免闪烁引发不适，脉冲频率 1.4s 属安全区间）

---

## 七、Android 落地映射

### 7.1 `res/values/colors.xml`

```xml
<resources>
    <!-- 表面 -->
    <color name="bg_base">#0A0C10</color>
    <color name="bg_surface">#12151B</color>
    <color name="bg_surface_2">#1A1E26</color>
    <color name="bg_surface_3">#232833</color>
    <color name="border">#252A34</color>
    <color name="border_strong">#333A47</color>

    <!-- 文本 -->
    <color name="text_primary">#F2F4F7</color>
    <color name="text_secondary">#9AA3B2</color>
    <color name="text_tertiary">#7A8494</color>

    <!-- 语义 -->
    <color name="hr">#FF4D5E</color>
    <color name="hr_soft">#24FF4D5E</color>          <!-- 14% alpha -->
    <color name="accent">#3D8BFF</color>
    <color name="accent_solid">#2563EB</color>
    <color name="accent_soft">#293D8BFF</color>      <!-- 16% alpha -->
    <color name="ok">#22C55E</color>
    <color name="ok_soft">#2422C55E</color>
    <color name="warn">#F59E0B</color>
    <color name="warn_soft">#26F59E0B</color>
    <color name="danger">#EF4444</color>
    <color name="danger_soft">#24EF4444</color>
    <color name="danger_text">#FF8A8A</color>
</resources>
```

浅色主题放 `res/values-night` 的**反向**目录（`values` 放浅色、`values-night` 放深色），或按当前项目习惯统一 —— 建议后者：`values/themes.xml` 定义浅色，`values-night/themes.xml` 定义深色。

### 7.2 `res/values/dimens.xml`

```xml
<resources>
    <!-- 间距 4pt -->
    <dimen name="sp_1">4dp</dimen>  <dimen name="sp_2">8dp</dimen>
    <dimen name="sp_3">12dp</dimen> <dimen name="sp_4">16dp</dimen>
    <dimen name="sp_5">20dp</dimen> <dimen name="sp_6">24dp</dimen>
    <dimen name="sp_8">32dp</dimen>

    <!-- 圆角 -->
    <dimen name="radius_input">8dp</dimen>
    <dimen name="radius_btn">12dp</dimen>
    <dimen name="radius_card">16dp</dimen>
    <dimen name="radius_sheet">24dp</dimen>

    <!-- 尺寸 -->
    <dimen name="page_padding">16dp</dimen>
    <dimen name="appbar_h">52dp</dimen>
    <dimen name="tabbar_h">62dp</dimen>
    <dimen name="touch_min">48dp</dimen>
    <dimen name="btn_h">46dp</dimen>
    <dimen name="btn_h_lg">56dp</dimen>
    <dimen name="list_row_min_h">52dp</dimen>
    <dimen name="pill_h">26dp</dimen>
</resources>
```

### 7.3 字号（`res/values/dimens.xml` 或直接 sp）

| 用途 | sp | 字重 |
|---|---|---|
| 心率数值 | 60 | `textStyle="bold"` |
| 页标题 | 20 | bold |
| 卡片标题 | 16 | bold（`textStyle="bold"` 或字重 600） |
| 正文 | 14 | normal |
| 标签 | 12 | `textStyle="bold"` |
| 说明 | 11 | normal |

### 7.4 实现路径与依赖取舍

当前工程仅依赖 `sdk-camera`（刻意规避 `sdk-media` 的 155MB 原生库），因此**不建议为了这几个组件引入 Material Components**。可选方案：

| 组件 | 无新依赖实现 | 若允许引入 Material |
|---|---|---|
| 底部 Tab | 自定义 `LinearLayout` + 选中态切换（原型即此方案，样式完全可控） | `BottomNavigationView` |
| 心率环 | 自定义 `View`，`Canvas.drawArc` + `Paint.setStrokeCap(ROUND)`，外加 `ValueAnimator` 平滑过渡 | — |
| 区间带 | 自定义 `View`，`drawRoundRect` 三段 + `clipPath` 统一圆角 | — |
| 滑块 | 自定义 `View`，`onTouchEvent` 换算比例（约 80 行） | `com.google.android.material.slider.Slider` |
| 分段控件 | `RadioGroup` + 自定义背景 selector | `MaterialButtonToggleGroup` |
| 列表 | 现有 `ListView` + 自定义 `BaseAdapter`（V14 已用 ListView，改造成本低） | `RecyclerView` |
| 折叠 | `LinearLayout` + `TransitionManager` 或 `ValueAnimator` 控制高度 | `MaterialCardView` |

**推荐落地顺序**（按收益/成本比排序）：
1. **素材页**（收益最大）—— 三步 stepper + 单选列表 + 固定操作条，把两个嵌套 `ScrollView` 拆掉
2. **控制台**（演示露脸最多）—— 心率环 + 区间带 + 主开关三件套
3. **诊断页** —— 把 `logText` 迁移过去，纯搬运
4. **设置页** —— 滑块替换裸 `EditText`，工作量最大，收益相对最小
5. 全局换色 —— 替换 `colors.xml` 即可，但**必须最后做**，否则中途截图会新旧混杂

---

## 八、与 V14 的兼容注意

1. **不要删除 V14 的信息，只改变它的位置** —— 例如「可填范围 30~250 bpm」这条说明，从常驻正文改为滑块的 `contentDescription` + 越界时的即时提示。信息不丢，只是不再占版面。
2. **上传地址输入框**保留在设置的折叠区，并显示当前值的完整 URL（V14 直接暴露输入框，视觉噪声大且易误改）。
3. **权限被拒时的界面**：V14 是静默失败（界面一行日志不打）。V15 要求在诊断页自检行显示 `✗` 并给出「去设置」跳转。
4. **`armed` / `capturing` 的显示分离**：界面所有「在不在拍」的判断只读 `armed`；`capturing` 仅用于在会话列表里显示相机自报的录制时长。
5. **重装 APK 会清空 SharedPreferences** —— 上线前建议增加设置项的导入/导出（诊断页或设置页末），避免演示前丢配置。

---

**交付物**
- `设计/心跳相机V15-界面原型.html` —— 高保真五屏 + 设计系统，可切换明暗主题
- `设计/心跳相机V15-UI设计规范.md` —— 本文件，可直接交付开发
