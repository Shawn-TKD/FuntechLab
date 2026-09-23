package com.insta360.heartbeat.ui

import android.Manifest
import android.bluetooth.BluetoothAdapter
import android.bluetooth.BluetoothDevice
import android.bluetooth.BluetoothManager
import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.content.pm.PackageManager
import android.content.res.ColorStateList
import android.content.res.Configuration
import android.graphics.Typeface
import android.os.Build
import android.os.Bundle
import android.text.Spannable
import android.text.SpannableStringBuilder
import android.text.style.ForegroundColorSpan
import android.view.View
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import android.widget.Toast
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.appcompat.app.AppCompatDelegate
import androidx.core.content.ContextCompat
import androidx.core.view.WindowCompat
import com.google.android.material.button.MaterialButton
import com.google.android.material.button.MaterialButtonToggleGroup
import com.google.android.material.materialswitch.MaterialSwitch
import com.google.android.material.progressindicator.LinearProgressIndicator
import com.google.android.material.slider.Slider
import com.google.android.material.bottomnavigation.BottomNavigationView
import com.insta360.heartbeat.CrashLogger
import com.insta360.heartbeat.HeartbeatApp
import com.insta360.heartbeat.R
import com.insta360.heartbeat.camera.CameraController
import com.insta360.heartbeat.camera.CaptureMode
import com.insta360.heartbeat.settings.AppSettings
import com.insta360.heartbeat.ui.widget.HrRingView
import com.insta360.heartbeat.ui.widget.HrZoneBandView
import com.insta360.heartbeat.upload.ClipUploadManager
import kotlin.math.roundToInt

/**
 * 主界面：请求权限 → 初始化 SDK → 编排心率+阈值+相机。
 */
class MainActivity : AppCompatActivity() {

    // ---- V15 四 Tab 外壳 ----
    private lateinit var navBar: BottomNavigationView
    private lateinit var appBarTitle: TextView
    private lateinit var pillCamera: TextView
    private lateinit var pillHr: TextView
    private lateinit var panelConsole: View
    private lateinit var panelMedia: View
    private lateinit var panelSettings: View
    private lateinit var panelDiagnostics: View

    // ---- 控制台 ----
    private lateinit var hrRing: HrRingView
    private lateinit var zoneBand: HrZoneBandView
    private lateinit var zoneSafe: TextView
    private lateinit var zoneHyst: TextView
    private lateinit var zoneTrig: TextView
    private lateinit var zoneNow: TextView
    private lateinit var btnMainSwitch: MaterialButton
    private lateinit var mainSwitchHint: TextView
    private lateinit var sessionList: LinearLayout
    private lateinit var sessionEmpty: TextView
    private lateinit var hrSourceBody: LinearLayout
    private lateinit var btnToggleHrSource: MaterialButton

    // ---- 通用 ----
    private lateinit var tvHr: TextView
    private lateinit var tvCapture: TextView
    private lateinit var tvDevice: TextView
    private lateinit var tvLog: TextView
    private lateinit var logScroll: ScrollView
    private lateinit var scanHint: TextView
    private lateinit var deviceList: LinearLayout

    // ---- 设置 ----
    private lateinit var modeGroup: MaterialButtonToggleGroup
    private lateinit var sliderOn: Slider
    private lateinit var sliderOff: Slider
    private lateinit var onValue: TextView
    private lateinit var offValue: TextView
    private lateinit var hysteresisHint: TextView
    private lateinit var intervalInput: EditText
    private lateinit var btnIntervalMinus: MaterialButton
    private lateinit var btnIntervalPlus: MaterialButton
    private lateinit var intervalChips: LinearLayout
    private lateinit var settingsSummary: TextView
    private lateinit var toggleCameraSetup: LinearLayout
    private lateinit var cameraSetupBody: LinearLayout
    private lateinit var chevronCamera: TextView
    private lateinit var toggleServerUrl: LinearLayout
    private lateinit var serverUrlBody: LinearLayout
    private lateinit var chevronServer: TextView
    private lateinit var serverUrlCurrent: TextView
    private lateinit var uploadUrlInput: EditText

    // ---- 素材 ----
    private lateinit var segMedia: MaterialButtonToggleGroup
    private lateinit var secCamera: LinearLayout
    private lateinit var secServer: LinearLayout
    private lateinit var uploadStage: TextView
    private lateinit var uploadProgress: LinearProgressIndicator
    private lateinit var mediaListHint: TextView
    private lateinit var mediaListScroll: ScrollView
    private lateinit var mediaListContainer: LinearLayout
    private lateinit var btnUploadSelected: Button
    private lateinit var btnRefreshServer: Button
    private lateinit var serverListHint: TextView
    private lateinit var serverListScroll: ScrollView
    private lateinit var serverListContainer: LinearLayout

    // ---- 诊断 ----
    private lateinit var selfCheckContainer: LinearLayout
    private lateinit var switchLightTheme: MaterialSwitch

    /** 当前心率（0 = 无数据）。给状态胶囊与区间带文案复用。 */
    private var currentHr = 0

    /** 相机素材列表（点「刷新素材列表」后填充，已按「视频优先 + 最新在前」排好）。 */
    private var mediaItems: List<CameraController.MediaItem> = emptyList()

    /** 当前选中的素材在 [mediaItems] 里的下标。-1 = 还没选。 */
    private var selectedMediaIndex = -1

    /**
     * ⚠️ 必须用 lateinit + 在 onCreate 里初始化，**不能**写成 `= HeartbeatOrchestrator(this)`。
     *
     * 原因：字段初始化器在 Activity **构造期**执行，那时 Activity 还没 attach，
     * `getSystemService()` 会抛 "System services not available to Activities before onCreate()"，
     * 而 HeartbeatOrchestrator 构造时会 new HeartRateScanner(context)，
     * 后者立刻调 getSystemService(BLUETOOTH_SERVICE) → 冷启动必崩。
     */
    private lateinit var orchestrator: HeartbeatOrchestrator

    /**
     * 终端日志的行缓冲（**分级着色**）。
     *
     * 存 (文本, 颜色资源) 而不是直接拼 Spannable，是为了能按行淘汰旧行 ——
     * 直接对 SpannableStringBuilder 做头部裁剪会把跨行的 span 剪坏。
     * 上限 [MAX_LOG_LINES] 行，超出丢最旧的，避免长时间运行后每次 log 都要
     * 重排一个越来越大的富文本。
     */
    private val logLines = ArrayDeque<Pair<String, Int>>()

    private val hrDevices = LinkedHashMap<String, ScannedRow>()

    /**
     * 用户是否已经点选了设备、主动放弃了本轮扫描。
     *
     * 为什么要这个标志：点选设备后会立刻 `stopScan()`，而 BLE 扫描结果是走
     * `Handler` 投递到主线程的——点击处理和扫描回调**都在主线程队列里**，
     * 完全可能有几条已经在队列里排队的扫描结果，在 `stopScan()` 之后才被执行。
     * 没有这个标志的话，它们会把刚刚清空/隐藏的列表又画出来。
     */
    private var scanAbandoned = false

    // ---- 扫描列表渲染节流（防 ANR） ----
    //
    // ⚠️ 为什么必须节流：BLE 扫描回调 `onScanResult` 是**在主线程**触发的，
    // 而 SCAN_MODE_LOW_LATENCY 下周围几十台设备每秒会产生**几百次**回调。
    // 早期版本每收到一个回调就把整个列表 `removeAllViews()` 再逐台重建 TextView，
    // 主线程瞬间被压死 → MIUI 直接弹「心跳控制相机无响应」（ANR）。
    //
    // 现在两道防线：
    //   1. **节流**：最多每 [RENDER_THROTTLE_MS] 重绘一次（合并高频回调）；
    //   2. **签名比对**：内容没变就完全不动视图；
    //   3. **复用行视图**：只更新文字，不重复 new TextView。
    private val renderHandler = android.os.Handler(android.os.Looper.getMainLooper())
    private var renderScheduled = false
    private var lastRenderSignature: String? = null
    private val rowViews = HashMap<String, LinearLayout>()

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)

        // ⚠️ 用单例：编排者持有相机会话 + BLE 连接，必须活过 Activity 重建。
        // 旧的写法 `HeartbeatOrchestrator(this)` 会让每次折叠/旋转都把链路拆掉（真机踩过）。
        orchestrator = HeartbeatOrchestrator.get(this)

        // ---- V15 四 Tab 外壳 ----
        navBar = findViewById(R.id.navBar)
        appBarTitle = findViewById(R.id.appBarTitle)
        pillCamera = findViewById(R.id.pillCamera)
        pillHr = findViewById(R.id.pillHr)
        panelConsole = findViewById(R.id.panelConsole)
        panelMedia = findViewById(R.id.panelMedia)
        panelSettings = findViewById(R.id.panelSettings)
        panelDiagnostics = findViewById(R.id.panelDiagnostics)

        // ---- 控制台 ----
        hrRing = findViewById(R.id.hrRing)
        zoneBand = findViewById(R.id.zoneBand)
        zoneSafe = findViewById(R.id.zoneSafe)
        zoneHyst = findViewById(R.id.zoneHyst)
        zoneTrig = findViewById(R.id.zoneTrig)
        zoneNow = findViewById(R.id.zoneNow)
        btnMainSwitch = findViewById(R.id.btnMainSwitch)
        mainSwitchHint = findViewById(R.id.mainSwitchHint)
        sessionList = findViewById(R.id.sessionList)
        sessionEmpty = findViewById(R.id.sessionEmpty)
        hrSourceBody = findViewById(R.id.hrSourceBody)
        btnToggleHrSource = findViewById(R.id.btnToggleHrSource)

        // ---- 通用 ----
        tvHr = findViewById(R.id.hrValue)
        tvCapture = findViewById(R.id.captureState)
        tvDevice = findViewById(R.id.deviceState)
        tvLog = findViewById(R.id.logText)
        logScroll = findViewById(R.id.logScroll)
        scanHint = findViewById(R.id.scanHint)
        deviceList = findViewById(R.id.deviceList)

        // ---- 设置 ----
        modeGroup = findViewById(R.id.modeGroup)
        sliderOn = findViewById(R.id.sliderOn)
        sliderOff = findViewById(R.id.sliderOff)
        onValue = findViewById(R.id.onValue)
        offValue = findViewById(R.id.offValue)
        hysteresisHint = findViewById(R.id.hysteresisHint)
        intervalInput = findViewById(R.id.intervalInput)
        btnIntervalMinus = findViewById(R.id.btnIntervalMinus)
        btnIntervalPlus = findViewById(R.id.btnIntervalPlus)
        intervalChips = findViewById(R.id.intervalChips)
        settingsSummary = findViewById(R.id.settingsSummary)
        toggleCameraSetup = findViewById(R.id.toggleCameraSetup)
        cameraSetupBody = findViewById(R.id.cameraSetupBody)
        chevronCamera = findViewById(R.id.chevronCamera)
        toggleServerUrl = findViewById(R.id.toggleServerUrl)
        serverUrlBody = findViewById(R.id.serverUrlBody)
        chevronServer = findViewById(R.id.chevronServer)
        serverUrlCurrent = findViewById(R.id.serverUrlCurrent)
        uploadUrlInput = findViewById(R.id.uploadUrlInput)

        // ---- 素材 ----
        segMedia = findViewById(R.id.segMedia)
        secCamera = findViewById(R.id.secCamera)
        secServer = findViewById(R.id.secServer)
        uploadStage = findViewById(R.id.uploadStage)
        uploadProgress = findViewById(R.id.uploadProgress)
        mediaListHint = findViewById(R.id.mediaListHint)
        mediaListScroll = findViewById(R.id.mediaListScroll)
        mediaListContainer = findViewById(R.id.mediaListContainer)
        btnUploadSelected = findViewById(R.id.btnUploadSelected)
        btnRefreshServer = findViewById(R.id.btnRefreshServer)
        serverListHint = findViewById(R.id.serverListHint)
        serverListScroll = findViewById(R.id.serverListScroll)
        serverListContainer = findViewById(R.id.serverListContainer)

        // ---- 诊断 ----
        selfCheckContainer = findViewById(R.id.selfCheckContainer)
        switchLightTheme = findViewById(R.id.switchLightTheme)

        uploadUrlInput.setText(orchestrator.currentUploadUrl())
        configureUploader()

        setupTabs()
        setupCollapsibles()
        setupSegment()
        setupSliders()
        setupIntervalControls()
        setupDiagnostics()
        applySystemBarAppearance()

        wireButtons()
        loadSettingsIntoUi()
        wireModeSelector()
        checkPermissions()
        showLastCrashIfAny()
    }

    // ═══════════════ V15 外壳：Tab / 折叠 / 分段 ═══════════════

    private fun setupTabs() {
        navBar.setOnItemSelectedListener { item ->
            val index = when (item.itemId) {
                R.id.tab_media -> 1
                R.id.tab_settings -> 2
                R.id.tab_diag -> 3
                else -> 0
            }
            showPanel(index)
            true
        }
        navBar.selectedItemId = R.id.tab_console
        showPanel(0)

        // 深链：直接落到指定 Tab。
        //   adb shell am start -n com.insta360.heartbeat/.ui.MainActivity --ei tab 2
        // 两个用途：① 无 adb 点击能力时的自动化验证（本机 MIUI 禁了 input tap）；
        //          ② 演示时可以直接跳到设置/诊断页，不用现场点。
        val requested = intent?.getIntExtra(EXTRA_TAB, -1) ?: -1
        if (requested in TAB_IDS.indices) {
            navBar.selectedItemId = TAB_IDS[requested]
        }
    }

    /**
     * 切换 Tab。
     *
     * 用「同一 Activity 切面板可见性」而不是 Fragment —— 本 Activity 握着
     * SDK 相机会话 + BLE GATT 这类长连接，Fragment 的生命周期会带来
     * 「切 Tab 就把链路拆掉」的风险，这正是 V12 修过的坑。
     */
    private fun showPanel(index: Int) {
        val panels = listOf(panelConsole, panelMedia, panelSettings, panelDiagnostics)
        panels.forEachIndexed { i, v -> v.visibility = if (i == index) View.VISIBLE else View.GONE }
        appBarTitle.text = TAB_TITLES.getOrElse(index) { TAB_TITLES[0] }

        when (index) {
            0 -> {
                refreshMainSwitch()
                updateZoneNow(currentHr)
            }

            3 -> {
                refreshSelfCheck()
                // 日志在诊断页：切进来的那一刻滚到底，用户看到的就是最新一条
                logScroll.post { logScroll.fullScroll(View.FOCUS_DOWN) }
            }
        }
    }

    /** 折叠区：标题行点击展开/收起。chevron 用字符旋转表达状态（配色/文字/图标三重编码）。 */
    private fun setupCollapsibles() {
        toggleCameraSetup.setOnClickListener { toggleCollapse(cameraSetupBody, chevronCamera) }
        toggleServerUrl.setOnClickListener { toggleCollapse(serverUrlBody, chevronServer) }
    }

    private fun toggleCollapse(body: View, chevron: TextView) {
        val show = body.visibility != View.VISIBLE
        body.visibility = if (show) View.VISIBLE else View.GONE
        chevron.text = if (show) "⌄" else "›"
        if (body === serverUrlBody) refreshServerUrlDisplay()
    }

    /** 素材页的分段：相机素材 / 已上传任务。 */
    private fun setupSegment() {
        segMedia.addOnButtonCheckedListener { _, checkedId, isChecked ->
            if (!isChecked) return@addOnButtonCheckedListener
            val camera = checkedId == R.id.segCamera
            secCamera.visibility = if (camera) View.VISIBLE else View.GONE
            secServer.visibility = if (camera) View.GONE else View.VISIBLE
        }
    }

    /** 主开关提示行里的当前上传地址（折叠区里显示完整 URL，避免误改）。 */
    private fun refreshServerUrlDisplay() {
        val url = uploadUrlInput.text.toString().trim().ifBlank { orchestrator.currentUploadUrl() }
        serverUrlCurrent.text = if (url.isBlank()) "当前生效：未配置" else "当前生效：$url"
    }

    /**
     * 系统栏图标明暗。
     *
     * 深色主题要浅色图标，否则状态栏上的时间/电量会看不见。
     * 注意 manifest 里 `configChanges` 含 `uiMode`，所以改主题后系统**不会**自动重建，
     * 必须由 [switchLightTheme] 的监听手动 `recreate()`。
     */
    private fun applySystemBarAppearance() {
        val night = (resources.configuration.uiMode and Configuration.UI_MODE_NIGHT_MASK) ==
            Configuration.UI_MODE_NIGHT_YES
        runCatching {
            WindowCompat.getInsetsController(window, window.decorView).apply {
                isAppearanceLightStatusBars = !night
                isAppearanceLightNavigationBars = !night
            }
        }
    }

    /**
     * 若上次运行发生过崩溃，把堆栈开头几行显示在日志区。
     * 真机无 adb 时，这是最省事的排查入口（完整堆栈见外部私有目录 crash.txt）。
     */
    private fun showLastCrashIfAny() {
        val crash = CrashLogger.readLast(this) ?: return
        val tail = crash.lines().filter { it.isNotBlank() }.takeLast(12).joinToString("\n")
        log("⚠ 检测到上次崩溃记录（完整内容见 crash.txt）:\n$tail")
    }

    private fun wireButtons() {
        findViewById<Button>(R.id.btnConnectCamera).setOnClickListener {
            log("点击连接相机（请先确认手机已连上相机的 WiFi 热点）")
            orchestrator.connectCamera()
        }
        findViewById<Button>(R.id.btnActivate).setOnClickListener {
            val appId = findViewById<EditText>(R.id.appIdInput).text.toString().trim()
            val secret = findViewById<EditText>(R.id.secretKeyInput).text.toString().trim()
            if (appId.isBlank() || secret.isBlank()) {
                toast("请先填写激活 AppId 和 SecretKey")
                return@setOnClickListener
            }
            log("发起激活...")
            orchestrator.activateCamera(appId, secret)
        }
        // 心率源扫描默认折叠：它是配置动作，展开后列表很长，
        // 常驻会把控制台「心率环」这个唯一焦点冲淡。
        btnToggleHrSource.setOnClickListener {
            val show = hrSourceBody.visibility != View.VISIBLE
            hrSourceBody.visibility = if (show) View.VISIBLE else View.GONE
            btnToggleHrSource.text = if (show) "心率源 ▴" else "心率源 ▾"
        }
        findViewById<Button>(R.id.btnScanBle).setOnClickListener {
            log("开始扫描。请先在 FR255 上开启「心率广播」，再从下方列表点选你的手表。")
            scanForHeartRateDevices()
        }
        findViewById<Button>(R.id.btnSimulate).setOnClickListener {
            log("启动模拟心率（无需设备）。观察心率冲过 ${orchestrator.currentThresholds().first} bpm 后相机应自动${modeVerb()}。")
            orchestrator.toggleSimulation(true)
            refreshMainSwitch()
        }

        // ---- 控制台主开关：一个按钮承担启停 ----
        // 「在不在跑」只认 App 自己的心率源状态（模拟中 / 真设备已连），
        // **不看相机自报的 isWorking()** —— 间隔/延时类模式下两次快门之间它会返回 false，
        // 用它判断会漏发停止命令（V14 踩过：界面显示已暂停、相机还在拍）。
        btnMainSwitch.setOnClickListener {
            val snap = orchestrator.snapshotForUi()
            if (isHeartRateRunning(snap)) {
                doStopHeartRate()
            } else {
                if (!snap.cameraConnected) {
                    toast("请先连接相机")
                    log("⚠ 相机未连接。先点「连接相机」，相机连上后主开关才会可用。")
                    return@setOnClickListener
                }
                log("启动心率触发（模拟心率）。观察心率冲过 ${orchestrator.currentThresholds().first} bpm 后相机应自动${modeVerb()}。")
                orchestrator.toggleSimulation(true)
                refreshMainSwitch()
            }
        }

        // 「停止」= 只停心率侧；「断开」= 只断相机。两者刻意解耦：
        // 演示时重连相机要切系统 WiFi + 等 SDK 握手，代价远高于重启心率源，
        // 所以停止心率不该顺手把相机也拆了（那是旧版「停止/断开」一个按钮干三件事的问题）。
        findViewById<Button>(R.id.btnStopHeartRate).setOnClickListener { doStopHeartRate() }

        findViewById<Button>(R.id.btnDisconnectCamera).setOnClickListener {
            orchestrator.disconnectCamera()
            tvCapture.text = "相机: 未连接"
            log("已断开相机连接。心率源不受影响，仍可继续扫描或模拟。")
            refreshMainSwitch()
            updatePills()
            refreshSelfCheckIfVisible()
        }
        findViewById<Button>(R.id.btnSaveSettings).setOnClickListener {
            saveSettingsFromUi()
        }
        findViewById<Button>(R.id.btnUploadClip).setOnClickListener {
            onUploadButtonClicked()
        }
        findViewById<Button>(R.id.btnRefreshMedia).setOnClickListener {
            onRefreshMediaClicked()
        }
        btnUploadSelected.setOnClickListener {
            onUploadSelectedClicked()
        }
        btnRefreshServer.setOnClickListener {
            onRefreshServerClicked()
        }
    }

    /** 「心率触发是否开着」= 模拟心率在跑，或真心率设备已连。 */
    private fun isHeartRateRunning(snap: HeartbeatOrchestrator.UiSnapshot): Boolean =
        snap.simulating || snap.heartRateDeviceName != null

    /**
     * 「停止」的完整动作（控制台主开关与设置页的停止按钮共用）。
     *
     * 必须把 tvDevice / tvCapture / 心率环 / 区间带一起复位 —— 否则界面会留着
     * 上一轮的读数，用户会以为心率源还在跑。
     */
    private fun doStopHeartRate() {
        orchestrator.stopHeartRate()
        // stopHeartRate 内部已推过一次连接态，这里再调一次是幂等的保险：
        // 万一日后有人改掉内部的回调推送，UI 也不会卡在「已连接」。
        applyHeartRateConnectionState(connected = false, deviceName = null)
        tvDevice.text = "心率源: 未连接"
        tvCapture.text =
            if (orchestrator.snapshotForUi().cameraConnected) {
                "相机: 已停止（WiFi 连接保留）"
            } else {
                "相机: 未连接"
            }
        currentHr = 0
        tvHr.text = "--"
        hrRing.clearHeartRate()
        zoneBand.clearHeartRate()
        updateZoneNow(0)
        log("已停止心率触发（模拟/手表均已停）。相机连接保留，可再次启动模拟或扫描设备。")
        refreshMainSwitch()
        updatePills()
    }

    /**
     * 主开关的文案与状态（设计规范里的三个关键状态分支）。
     *
     *  1. 未连相机且心率源没跑 → **置灰** + hint 指向「连接相机」
     *  2. 已连接、未启动 → accent 实底「启动心率触发」
     *  3. 触发中 → 危险弱底「停止触发并结束录制」
     */
    private fun refreshMainSwitch() {
        val snap = orchestrator.snapshotForUi()
        val running = isHeartRateRunning(snap)

        if (running) {
            btnMainSwitch.text = "停止触发并结束录制"
            btnMainSwitch.backgroundTintList = ColorStateList.valueOf(getColor(R.color.danger_soft))
            btnMainSwitch.setTextColor(getColor(R.color.danger_text))
        } else {
            btnMainSwitch.text = "启动心率触发"
            btnMainSwitch.backgroundTintList = ColorStateList.valueOf(getColor(R.color.accent_solid))
            btnMainSwitch.setTextColor(android.graphics.Color.WHITE)
        }

        val enabled = running || snap.cameraConnected
        btnMainSwitch.isEnabled = enabled
        btnMainSwitch.alpha = if (enabled) 1f else 0.42f

        mainSwitchHint.text = when {
            running -> "心率源运行中，触发由阈值自动控制（≥ ${orchestrator.currentThresholds().first} bpm 开拍）"
            snap.cameraConnected -> "相机已连接，可以启动"
            else -> "需要先连接相机"
        }
    }

    /**
     * AppBar 的两个状态胶囊。
     *
     * 三重编码：颜色 + 符号 + 文字。
     * 心率红**只**出现在心率胶囊上，不兼作危险色 ——
     * 否则用户无法判断「红色是心跳还是出错了」。
     */
    private fun updatePills() {
        val snap = runCatching { orchestrator.snapshotForUi() }.getOrNull()

        val cameraOn = snap?.cameraConnected == true
        pillCamera.text = if (cameraOn) "● 相机" else "○ 相机"
        pillCamera.setBackgroundResource(if (cameraOn) R.drawable.bg_pill_ok else R.drawable.bg_pill_neutral)
        pillCamera.setTextColor(getColor(if (cameraOn) R.color.ok else R.color.text_secondary))

        val hrOn = currentHr > 0
        pillHr.text = if (hrOn) "♥ $currentHr" else "♥ --"
        pillHr.setBackgroundResource(if (hrOn) R.drawable.bg_pill_hr else R.drawable.bg_pill_neutral)
        pillHr.setTextColor(getColor(if (hrOn) R.color.hr else R.color.text_secondary))
    }

    private fun refreshSelfCheckIfVisible() {
        if (panelDiagnostics.visibility == View.VISIBLE) refreshSelfCheck()
    }

    // ═══════════════ 控制台：区间带文案 ═══════════════

    /**
     * 把「当前心率落在哪个区、离触发线还有多远」写成一句话。
     *
     * 这是区间带的可读性兜底：色带解决「一眼看出位置」，
     * 这行文字解决「色弱用户 / 想确认具体差值」的需求（三重编码里的文字这一重）。
     */
    private fun updateZoneNow(hr: Int) {
        val (on, off) = orchestrator.currentThresholds()
        zoneNow.text = when {
            hr <= 0 -> "等待心率数据…"
            hr >= on -> "已进入触发区（≥ $on bpm）"
            hr > off -> "迟滞带：需回落到 $off bpm 以下才会重新触发"
            else -> "安全区：再升 ${on - hr} bpm 触发"
        }
    }

    /** 区间带 + 环上的阈值刻度 + 三个区段标签一起刷新。 */
    private fun syncThresholdVisuals() {
        val (on, off) = orchestrator.currentThresholds()
        hrRing.setThresholds(on, off)
        zoneBand.setThresholds(on, off)
        zoneSafe.text = "安全区 < $off"
        zoneHyst.text = "迟滞带 $off~$on"
        zoneTrig.text = "触发区 ≥ $on"
    }

    // ---- 素材列表（选一条 → 上传这条） ----

    /**
     * 「① 刷新素材列表」：只把相机里的素材读出来展示，不下载不上传。
     * 再点一次则是取消（和上传按钮一样，忙时同一个按钮兼作取消）。
     */
    private fun onRefreshMediaClicked() {
        if (orchestrator.isUploading()) {
            orchestrator.cancelUpload()
            updateUploadButton(false)
            return
        }
        val url = uploadUrlInput.text.toString().trim()
        if (url.isNotBlank()) orchestrator.saveUploadUrl(url)
        log("刷新相机素材列表…")
        mediaListHint.text = "正在读取相机素材列表…"
        orchestrator.refreshMediaList()
    }

    /** 「上传选中素材」：把列表里选中的那一条跑完「下载 → 转 MP4 → 上传」。 */
    private fun onUploadSelectedClicked() {
        if (orchestrator.isUploading()) {
            orchestrator.cancelUpload()
            updateUploadButton(false)
            return
        }
        val item = mediaItems.getOrNull(selectedMediaIndex)
        if (item == null) {
            toast("请先「刷新相机素材列表」，再从列表里点一行选中")
            log("⚠ 还没选中素材。先点「刷新相机素材列表」，再点列表里的一行。")
            return
        }
        val url = uploadUrlInput.text.toString().trim()
        if (url.isBlank()) {
            toast("请先填写服务器上传地址")
            log("⚠ 没填上传地址。测试时可以用 adb reverse：\nadb reverse tcp:8000 tcp:8000\n地址填 http://127.0.0.1:8000/upload")
            return
        }
        log("准备上传选中素材 → $url")
        updateUploadButton(true)
        orchestrator.uploadMediaItem(item, url)
    }

    /**
     * 「③ 查看已上传任务」：逐条查询**本机上传过**的任务状态
     * （上传令牌是受限凭据，不允许列全部，只能按已知 id 查）。
     * 纯只读，不碰相机、不影响上传链路。
     */
    private fun onRefreshServerClicked() {
        val url = uploadUrlInput.text.toString().trim()
        if (url.isBlank()) {
            toast("请先填写服务器上传地址")
            log("⚠ 没填上传地址，「③ 查看已上传任务」需要先有服务器地址。")
            return
        }
        log("③ 查看已上传任务…")
        serverListHint.text = "正在查询已上传任务…"
        orchestrator.refreshServerList(url)
    }

    /**
     * 把已上传任务的状态铺到界面上。
     * 第一行是总计，之后是每条任务的状态行。
     */
    private fun renderServerList(rows: List<String>) {
        serverListContainer.removeAllViews()
        serverListScroll.visibility = if (rows.isEmpty()) View.GONE else View.VISIBLE

        rows.forEach { line ->
            val row = TextView(this).apply {
                text = line
                textSize = 12f
                setPadding(dpToPx(8), dpToPx(6), dpToPx(8), dpToPx(6))
                if (line.startsWith("📁") || line.startsWith("共 ") || line.startsWith("· [")) {
                    // 分组 / 汇总 / 任务行加粗，与下面的明细行区分开
                    setTypeface(typeface, android.graphics.Typeface.BOLD)
                    setTextColor(getColor(R.color.text_primary))
                } else {
                    setTextColor(getColor(R.color.text_secondary))
                }
            }
            serverListContainer.addView(row)
        }
    }

    /**
     * 把素材列表画到界面上。
     *
     * 顺序已由 [CameraController.listCameraMediaItems] 排好：**视频优先、各自最新在前**，
     * 所以第一行就是「刚录的那段」，默认直接选中它 —— 用户不用滚列表也能一键上传。
     *
     * 素材可能上百条（真机实测 148 条），所以只在刷新时创建一次 View，
     * 选中态的变化只改颜色/字号，不重建列表。
     */
    private fun renderMediaList(items: List<CameraController.MediaItem>) {
        mediaItems = items
        selectedMediaIndex = if (items.isEmpty()) -1 else 0
        mediaListContainer.removeAllViews()
        mediaListScroll.visibility = if (items.isEmpty()) View.GONE else View.VISIBLE

        items.forEachIndexed { index, item ->
            val row = TextView(this).apply {
                text = "${index + 1}. [${item.kindLabel}] ${item.name}"
                textSize = 12f
                setPadding(dpToPx(8), dpToPx(7), dpToPx(8), dpToPx(7))
                setOnClickListener { selectMediaItem(index) }
            }
            mediaListContainer.addView(row)
        }
        refreshMediaSelectionUi()
    }

    private fun selectMediaItem(index: Int) {
        if (index !in mediaItems.indices) return
        selectedMediaIndex = index
        refreshMediaSelectionUi()
        log("👆 已选中第 ${index + 1} 条：${mediaItems[index].name}")
    }

    /** 只更新选中态与提示文案（不重建列表）。 */
    private fun refreshMediaSelectionUi() {
        val picked = mediaItems.getOrNull(selectedMediaIndex)
        for (i in 0 until mediaListContainer.childCount) {
            val row = mediaListContainer.getChildAt(i) as? TextView ?: continue
            val on = i == selectedMediaIndex
            row.setTextColor(getColor(if (on) R.color.accent else R.color.text_secondary))
            // 选中态用「accent 弱底 + accent 描边」，和普通行（surface-2 + border）区分开，
            // 不再靠换字号来表达选中 —— 字号变化会引起列表重排、行高跳动。
            row.setBackgroundResource(if (on) R.drawable.bg_row_selected else R.drawable.bg_row_normal)
        }
        mediaListHint.text = when {
            mediaItems.isEmpty() -> "素材列表尚未读取。先点「刷新相机素材列表」，再从下面点一条视频。"
            picked == null -> mediaSummary()
            else -> "${mediaSummary()} · 已选：${picked.kindLabel} ${picked.name}"
        }
        btnUploadSelected.isEnabled = picked != null
        btnUploadSelected.alpha = if (picked != null) 1f else 0.6f
        refreshStepper()
    }

    private fun mediaSummary(): String {
        val videos = mediaItems.count { it.isVideo }
        return "共 ${mediaItems.size} 条素材（视频 $videos 条）· 已按最新在前排序"
    }

    private fun dpToPx(v: Int): Int = (v * resources.displayMetrics.density).toInt()

    /**
     * 上传按钮：**同一个按钮承担「开始上传」和「取消上传」两种职责**。
     *
     * 需求是「这个功能生成一个按钮操作」，所以没有另开取消按钮 ——
     * 上传几十 MB 的视频可能要好几十秒，忙的时候按钮文案变成「取消上传」，
     * 再点一次就是取消（并保留已下载的文件，下次可续传）。
     */
    private fun onUploadButtonClicked() {
        if (orchestrator.isUploading()) {
            orchestrator.cancelUpload()
            updateUploadButton(false)
            return
        }
        val url = uploadUrlInput.text.toString().trim()
        if (url.isBlank()) {
            toast("请先填写服务器上传地址")
            log("⚠ 没填上传地址。测试时可以用 adb reverse：\nadb reverse tcp:8000 tcp:8000\n地址填 http://127.0.0.1:8000/upload")
            return
        }
        log("准备上传最新一段素材 → $url")
        updateUploadButton(true)
        orchestrator.uploadLatestClip(url)
    }

    private fun updateUploadButton(busy: Boolean) {
        val btn = findViewById<Button>(R.id.btnUploadClip)
        btn.text = if (busy) "取消上传" else "或：自动上传最新一段视频"
        btn.isEnabled = true
        btn.alpha = if (busy) 0.85f else 1f
        // 忙时把「刷新/上传选中」也一并锁上，避免同时发起两条链路
        btnUploadSelected.isEnabled = !busy && mediaItems.getOrNull(selectedMediaIndex) != null
        btnUploadSelected.alpha = if (btnUploadSelected.isEnabled) 1f else 0.6f
        // 进度条：上传/下载/转码期间显示不确定进度条。
        // 刻意不用百分比 —— 链路里「下载(相机) → remux → 上传」三段耗时占比未知，
        // 硬凑一个百分比只会误导用户（宁可诚实地表示「在进行中」）。
        uploadProgress.visibility = if (busy) View.VISIBLE else View.GONE
        if (!busy) refreshStepper()
    }

    /**
     * 素材页的三步 stepper 状态。
     *
     * 步骤定义的依据是「用户此刻要完成什么」，不是「有哪些控件」：
     *   ① 读取列表 → ② 选中一条 → ③ 上传
     *
     * ⚠️ 列表为空时第二步**必须保持灰**，不能因为「有按钮可点」就点亮 ——
     * 否则会出现「看起来可以上传、点了却报错」的假象。
     */
    private fun refreshStepper() {
        val hasList = mediaItems.isNotEmpty()
        val hasPick = mediaItems.getOrNull(selectedMediaIndex) != null
        val busy = runCatching { orchestrator.isUploading() }.getOrDefault(false)

        paintStep(1, R.id.stepDot1, R.id.stepLabel1, done = hasList, current = !hasList)
        paintStep(2, R.id.stepDot2, R.id.stepLabel2, done = hasPick, current = hasList && !hasPick)
        paintStep(3, R.id.stepDot3, R.id.stepLabel3, done = false, current = busy || (hasPick && !busy))

        findViewById<View>(R.id.stepLine1).setBackgroundColor(
            getColor(if (hasList) R.color.ok else R.color.border)
        )
        findViewById<View>(R.id.stepLine2).setBackgroundColor(
            getColor(if (hasPick) R.color.ok else R.color.border)
        )
    }

    private fun paintStep(no: Int, dotId: Int, labelId: Int, done: Boolean, current: Boolean) {
        val dot = findViewById<TextView>(dotId)
        val label = findViewById<TextView>(labelId)
        dot.text = if (done) "✓" else no.toString()
        dot.setBackgroundResource(
            when {
                done -> R.drawable.bg_step_done
                current -> R.drawable.bg_step_current
                else -> R.drawable.bg_step_pending
            }
        )
        dot.setTextColor(
            getColor(
                when {
                    done -> R.color.ok
                    current -> R.color.accent
                    else -> R.color.text_tertiary
                }
            )
        )
        label.setTextColor(
            getColor(
                when {
                    done -> R.color.ok
                    current -> R.color.accent
                    else -> R.color.text_tertiary
                }
            )
        )
    }

    /**
     * 模式切换监听：切模式即时生效（不用先点保存）。
     * ⚠️ 必须在 [loadSettingsIntoUi] **之后**注册——否则回填选中态时会立刻触发一次保存。
     */
    private fun wireModeSelector() {
        modeGroup.addOnButtonCheckedListener { _, checkedId, isChecked ->
            if (isChecked && (checkedId == R.id.modeVideo || checkedId == R.id.modePhoto)) {
                saveSettingsFromUi(quiet = true)
            }
        }
    }

    // ═══════════════ 设置：滑块 / 间隔步进器 ═══════════════

    /**
     * 阈值滑块。
     *
     * ## 为什么要换成滑块
     *
     * V14 是两个裸 `EditText`：用户得先知道「能填多少」再手动输入，
     * 而且迟滞约束（停止 < 开拍）只能靠事后兜底 + 日志解释。
     * 滑块把「可填范围」变成**物理上不可越界**的事实，迟滞约束则用联动钳制实时表达。
     *
     * ## 钳制必须明说
     *
     * 停止阈值拖到 ≥ 开拍阈值时，实时压回「开拍 − 1」，
     * 并在滑块下方<b>立即</b>显示一行提示 —— **不静默改数**。
     * （V14 有过教训：用户填 7 却看到 5，会认定「被定死了」。）
     */
    private fun setupSliders() {
        sliderOn.addOnChangeListener { _, value, fromUser ->
            val v = value.roundToInt()
            onValue.text = "$v bpm"
            if (fromUser) {
                // 抬高开拍阈值后，若停止阈值不再小于它，一并下压
                if (sliderOff.value.roundToInt() >= v) {
                    val target = (v - 1).coerceAtLeast(AppSettings.MIN_HR).toFloat()
                    sliderOff.value = target
                    warnHysteresis("停止阈值已自动压回 ${target.roundToInt()} bpm（必须小于开拍阈值）")
                }
                refreshBandPreview()
            }
        }
        sliderOn.addOnSliderTouchListener(
            object : Slider.OnSliderTouchListener {
                override fun onStartTrackingTouch(slider: Slider) = Unit
                override fun onStopTrackingTouch(slider: Slider) {
                    saveSettingsFromUi(quiet = true, silentToast = true)
                }
            }
        )

        sliderOff.addOnChangeListener { _, value, fromUser ->
            val v = value.roundToInt()
            offValue.text = "$v bpm"
            if (fromUser) {
                val on = sliderOn.value.roundToInt()
                if (v >= on) {
                    val target = (on - 1).coerceAtLeast(AppSettings.MIN_HR).toFloat()
                    sliderOff.value = target
                    warnHysteresis("停止阈值已自动压回 ${target.roundToInt()} bpm（必须小于开拍阈值）")
                } else {
                    checkHysteresisWidth(on, v)
                }
                refreshBandPreview()
            }
        }
        sliderOff.addOnSliderTouchListener(
            object : Slider.OnSliderTouchListener {
                override fun onStartTrackingTouch(slider: Slider) = Unit
                override fun onStopTrackingTouch(slider: Slider) {
                    saveSettingsFromUi(quiet = true, silentToast = true)
                }
            }
        )
    }

    /** 把滑块当前值当作「预览值」推到区间带上（还没保存也能看见效果）。 */
    private fun refreshBandPreview() {
        val on = sliderOn.value.roundToInt()
        val off = sliderOff.value.roundToInt()
        hrRing.setThresholds(on, off)
        zoneBand.setThresholds(on, off)
        zoneSafe.text = "安全区 < $off"
        zoneHyst.text = "迟滞带 $off~$on"
        zoneTrig.text = "触发区 ≥ $on"
    }

    private fun warnHysteresis(text: String) {
        hysteresisHint.text = "⚠ $text"
        hysteresisHint.setTextColor(getColor(R.color.warn))
        log("⚠ $text")
    }

    /** 迟滞带过窄时提前预警（不必等到用户踩坑）。 */
    private fun checkHysteresisWidth(on: Int, off: Int) {
        if (on - off < 3) {
            hysteresisHint.text =
                "⚠ 迟滞带只有 ${on - off} bpm，过窄：心率在阈值附近波动时会反复开关相机。建议拉开到 5 bpm 以上。"
            hysteresisHint.setTextColor(getColor(R.color.warn))
        } else {
            hysteresisHint.text =
                "停止阈值必须小于开拍阈值（迟滞防抖）；两者间距小于 3 bpm 时可能在阈值附近反复开关。"
            hysteresisHint.setTextColor(getColor(R.color.text_tertiary))
        }
    }

    /**
     * 间隔控件：步进器（−/值/+）+ 快捷值。
     *
     * 为什么步进器比裸输入框好：间隔是个「调一调」的量，
     * 用户想要的是「再多 1 秒」，而不是「重新打一个数字」。
     * 快捷值取自相机固件**确实支持**的几个档位（见 go_ultra.json 的 lapse_time），
     * 点这些值能走固件自己的节拍（最准）；其它值会自动改用 App 侧定时器。
     */
    private fun setupIntervalControls() {
        btnIntervalMinus.setOnClickListener { bumpInterval(-1) }
        btnIntervalPlus.setOnClickListener { bumpInterval(1) }

        intervalChips.removeAllViews()
        QUICK_INTERVALS.forEach { v ->
            val chip = Button(this).apply {
                text = "${v}s"
                textSize = 11f
                minWidth = 0
                minimumWidth = 0
                isAllCaps = false
                setPadding(dpToPx(8), 0, dpToPx(8), 0)
                setBackgroundResource(R.drawable.bg_row_normal)
                setTextColor(getColor(R.color.text_primary))
                layoutParams = LinearLayout.LayoutParams(0, dpToPx(34), 1f).apply {
                    marginEnd = dpToPx(4)
                }
                setOnClickListener {
                    intervalInput.setText(v.toString())
                    saveSettingsFromUi(quiet = true, silentToast = true)
                }
            }
            intervalChips.addView(chip)
        }
    }

    private fun bumpInterval(delta: Int) {
        val cur = intervalInput.text.toString().trim().toIntOrNull()
            ?: orchestrator.currentSettings().intervalSeconds
        val next = (cur + delta).coerceIn(AppSettings.MIN_INTERVAL, AppSettings.MAX_INTERVAL)
        intervalInput.setText(next.toString())
        saveSettingsFromUi(quiet = true, silentToast = true)
    }

    // ═══════════════ 诊断页：自检 / 主题 / 日志操作 ═══════════════

    private fun setupDiagnostics() {
        val night = (resources.configuration.uiMode and Configuration.UI_MODE_NIGHT_MASK) ==
            Configuration.UI_MODE_NIGHT_YES
        switchLightTheme.isChecked = !night
        switchLightTheme.setOnCheckedChangeListener { _, light ->
            AppSettings(this).lightTheme = light
            AppCompatDelegate.setDefaultNightMode(
                if (light) AppCompatDelegate.MODE_NIGHT_NO else AppCompatDelegate.MODE_NIGHT_YES
            )
            // ⚠️ manifest 里 configChanges 含 uiMode → 系统不会自动重建，
            // 必须手动 recreate 一次，颜色资源才会按新配置重新解析。
            // recreate 走的是「配置变更」分支（isFinishing=false）→ detachUi，
            // 相机会话与 BLE 连接**不受影响**（V12 已修）。
            recreate()
        }

        findViewById<Button>(R.id.btnSelfCheck).setOnClickListener {
            refreshSelfCheck()
            toast("已重新自检")
        }

        findViewById<Button>(R.id.btnCopyLog).setOnClickListener {
            val text = logLines.joinToString("\n") { "• ${it.first}" }
            if (text.isBlank()) {
                toast("日志为空")
                return@setOnClickListener
            }
            val cm = getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager
            cm.setPrimaryClip(ClipData.newPlainText("heartbeat-log", text))
            toast("已复制 ${logLines.size} 行日志")
        }

        findViewById<Button>(R.id.btnClearLog).setOnClickListener {
            logLines.clear()
            tvLog.text = "日志:"
            toast("日志已清空")
        }
    }

    /**
     * 连接自检。
     *
     * V14 的失败是**静默**的（权限被拒时界面一行日志不打，用户完全不知道发生了什么）。
     * 这里把四项关键前提的当前状态直接铺出来，缺哪项一眼可见。
     */
    private fun refreshSelfCheck() {
        selfCheckContainer.removeAllViews()
        val snap = orchestrator.snapshotForUi()

        val missing = permissions.filter {
            ContextCompat.checkSelfPermission(this, it) != PackageManager.PERMISSION_GRANTED
        }
        addSelfCheckRow(
            ok = missing.isEmpty(),
            title = "权限",
            detail = if (missing.isEmpty()) {
                "全部已授予"
            } else {
                "缺少 ${missing.size} 项 —— 点这里去系统设置授予"
            },
            onClick = if (missing.isEmpty()) null else {
                {
                    runCatching {
                        startActivity(
                            android.content.Intent(
                                android.provider.Settings.ACTION_APPLICATION_DETAILS_SETTINGS,
                                android.net.Uri.fromParts("package", packageName, null),
                            )
                        )
                    }
                }
            },
        )

        addSelfCheckRow(
            ok = snap.cameraConnected,
            title = "相机",
            detail = if (snap.cameraConnected) "已连接" else "未连接（需先连相机 WiFi 热点）",
        )

        addSelfCheckRow(
            ok = snap.simulating || snap.heartRateDeviceName != null,
            title = "心率源",
            detail = when {
                snap.heartRateDeviceName != null -> "已连接 ${snap.heartRateDeviceName}"
                snap.simulating -> "模拟心率运行中"
                else -> "未启动"
            },
        )

        val url = uploadUrlInput.text.toString().trim().ifBlank { orchestrator.currentUploadUrl() }
        addSelfCheckRow(
            ok = url.isNotBlank(),
            title = "服务器",
            detail = if (url.isNotBlank()) url else "未配置上传地址",
        )
    }

    private fun addSelfCheckRow(ok: Boolean, title: String, detail: String, onClick: (() -> Unit)? = null) {
        val row = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = android.view.Gravity.CENTER_VERTICAL
            minimumHeight = dpToPx(48)
        }
        val dot = TextView(this).apply {
            text = if (ok) "✓" else "✕"
            textSize = 12f
            gravity = android.view.Gravity.CENTER
            setTextColor(getColor(if (ok) R.color.ok else R.color.danger))
            setBackgroundResource(if (ok) R.drawable.bg_dot_ok else R.drawable.bg_dot_bad)
        }
        row.addView(
            dot,
            LinearLayout.LayoutParams(dpToPx(22), dpToPx(22)).apply { marginEnd = dpToPx(12) },
        )

        val textWrap = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        textWrap.addView(
            TextView(this).apply {
                text = title
                textSize = 14f
                setTextColor(getColor(R.color.text_primary))
            }
        )
        textWrap.addView(
            TextView(this).apply {
                text = detail
                textSize = 11f
                setTextColor(getColor(R.color.text_tertiary))
            }
        )
        row.addView(textWrap, LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f))

        if (onClick != null) {
            row.isClickable = true
            row.setOnClickListener { onClick() }
        }
        selfCheckContainer.addView(row)
    }

    // ---- 拍摄设置 ----

    /** 把持久化设置回填到 UI（冷启动时调用）。 */
    private fun loadSettingsIntoUi() {
        val s = orchestrator.currentSettings()

        // ⚠️ 顺序：先设开拍再设停止。两个滑块的回调里只在 fromUser=true 时做联动钳制，
        // 所以程序化赋值不会触发钳制，回填是安全的。
        sliderOn.value = s.onThreshold.toFloat().coerceIn(MIN_HR_F, MAX_HR_F)
        sliderOff.value = s.offThreshold.toFloat().coerceIn(MIN_HR_F, MAX_HR_F)
        onValue.text = "${s.onThreshold} bpm"
        offValue.text = "${s.offThreshold} bpm"

        intervalInput.setText(s.intervalSeconds.toString())
        modeGroup.check(if (s.captureMode == CaptureMode.PHOTO) R.id.modePhoto else R.id.modeVideo)

        syncThresholdVisuals()
        checkHysteresisWidth(s.onThreshold, s.offThreshold)
        refreshSettingsSummary()
        updateIntervalEnabled()
        refreshServerUrlDisplay()
        updatePills()
    }

    /**
     * 读 UI 输入 → 保存 → 回显。
     *
     * 数值解析失败时退化为「当前已存值」，而不是报错中断：
     * 用户清空输入框再点保存是很常见的动作，直接报错体验很差。
     *
     * @param quiet 不写日志（拖动滑块/切模式这种高频动作用它，避免刷屏）
     * @param silentToast 不弹 toast（滑块松手时用；反馈已由数值/区间带/摘要给出）
     */
    private fun saveSettingsFromUi(quiet: Boolean = false, silentToast: Boolean = false) {
        val cur = orchestrator.currentSettings()
        val on = sliderOn.value.roundToInt()
        val off = sliderOff.value.roundToInt()
        val interval = intervalInput.text.toString().trim().toIntOrNull() ?: cur.intervalSeconds
        val mode = if (modeGroup.checkedButtonId == R.id.modePhoto) CaptureMode.PHOTO else CaptureMode.VIDEO

        val saved = orchestrator.saveSettings(on, off, interval, mode)
        val adjustments = describeAdjustments(on, off, interval, saved)

        // 回显实际生效值（可能被兜底调整过，比如 停止阈值 ≥ 开拍阈值 会被自动下压）
        sliderOn.value = saved.onThreshold.toFloat().coerceIn(MIN_HR_F, MAX_HR_F)
        sliderOff.value = saved.offThreshold.toFloat().coerceIn(MIN_HR_F, MAX_HR_F)
        onValue.text = "${saved.onThreshold} bpm"
        offValue.text = "${saved.offThreshold} bpm"
        intervalInput.setText(saved.intervalSeconds.toString())

        syncThresholdVisuals()
        checkHysteresisWidth(saved.onThreshold, saved.offThreshold)
        refreshSettingsSummary()
        updateIntervalEnabled()
        refreshMainSwitch()

        if (adjustments != null) {
            // 被调整过就明说，不要静默改数 —— 用户填了 7 却看到 5 会以为「被定死了」
            log("⚠ 有数值被有效范围兜底调整：$adjustments")
        }

        if (!quiet) {
            log(
                "设置已保存：${saved.captureMode.label}｜" +
                    "开拍 ${saved.onThreshold} bpm｜停止 ${saved.offThreshold} bpm" +
                    if (saved.captureMode.usesInterval) "｜间隔 ${saved.intervalSeconds} s" else ""
            )
            if (!silentToast) {
                toast(if (adjustments != null) "已保存（部分数值被兜底调整，见日志）" else "设置已保存")
            }
        }
    }

    /**
     * 间隔输入框。
     *
     * ⚠️ **始终可编辑**，不再按模式置灰。
     * 早期版本在录像模式下把这里 disable + 半透明，用户看到的就是「延时拍照的时间被定死了」。
     * 现在任何时候都能填，只是录像模式下不生效（摘要行会写清楚）。
     */
    private fun updateIntervalEnabled() {
        intervalInput.isEnabled = true
        intervalInput.alpha = 1f
    }

    /**
     * 把 UI 里填的值和「实际生效值」对比，列出被兜底调整过的项。
     *
     * 只在真的发生了调整时才提示，避免正常保存也弹一堆字。
     */
    private fun describeAdjustments(
        typedOn: Int,
        typedOff: Int,
        typedInterval: Int,
        saved: AppSettings.Saved,
    ): String? {
        val notes = mutableListOf<String>()
        if (typedOn != saved.onThreshold) {
            notes += "开拍阈值 $typedOn → ${saved.onThreshold}（有效范围 " +
                "${AppSettings.MIN_HR}~${AppSettings.MAX_HR} bpm）"
        }
        if (typedInterval != saved.intervalSeconds) {
            notes += "间隔 $typedInterval → ${saved.intervalSeconds} 秒（有效范围 " +
                "${AppSettings.MIN_INTERVAL}~${AppSettings.MAX_INTERVAL} 秒）"
        }
        if (typedOff != saved.offThreshold && typedOff != 0) {
            notes += "停止阈值 $typedOff → ${saved.offThreshold}（必须小于开拍阈值，否则迟滞失效、" +
                "相机会在阈值附近疯狂开关）"
        }
        return if (notes.isEmpty()) null else notes.joinToString("；")
    }

    /** 更新「当前生效」摘要行。 */
    private fun refreshSettingsSummary() {
        val (on, off) = orchestrator.currentThresholds()
        val s = orchestrator.currentSettings()
        settingsSummary.text = buildString {
            append("当前生效：")
            append(s.captureMode.label)
            append("｜心率 ≥ ").append(on).append(" 开始，< ").append(off).append(" 停止")
            if (s.captureMode.usesInterval) {
                append("｜每 ").append(s.intervalSeconds).append(" 秒一张")
            } else {
                append("｜间隔 ").append(s.intervalSeconds)
                    .append(" 秒（仅「间隔拍照」模式生效）")
            }
        }
    }

    // ---- 权限 ----

    /**
     * 需要申请的运行时权限。
     *
     * **定位权限的分档处理（别一刀切删掉）**：
     * manifest 里 BLUETOOTH_SCAN 带了 `neverForLocation`（我们只用广播里的服务
     * UUID/设备名认心率带，不做位置推断），所以 Android 12+ 不再要求定位权限 ——
     * 新机上也就不会弹「是否允许获取位置信息」。演示机是 Android 14，走的就是这条路。
     *
     * 但 Android 10/11（API 29/30）**没有**这个豁免：那两档上 BLE 扫描必须先有
     * ACCESS_FINE_LOCATION，否则 startScan 直接抛 SecurityException。
     * 所以这里按版本分档申请：31+ 不申请定位，29/30 申请定位。
     * （manifest 里也配套写了 maxSdkVersion="30"，两边必须一致，改一个就要改另一个。）
     */
    private val permissions =
        mutableListOf(
            Manifest.permission.BLUETOOTH_SCAN,
            Manifest.permission.BLUETOOTH_CONNECT,
            Manifest.permission.BLUETOOTH_ADVERTISE,
        ).apply {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
                add(Manifest.permission.POST_NOTIFICATIONS)
            }
            if (Build.VERSION.SDK_INT < Build.VERSION_CODES.S) {
                // Android 11 及以下：BLE 扫描的硬性前置条件
                add(Manifest.permission.ACCESS_FINE_LOCATION)
            }
        }

    private val permissionLauncher =
        registerForActivityResult(
            ActivityResultContracts.RequestMultiplePermissions()
        ) { result ->
            val allGranted = permissions.all { result[it] == true }
            if (allGranted) {
                (application as HeartbeatApp).initWithPermissionsGranted()
                log("SDK 已初始化，权限全部通过。")
                setupOrchestrator()
            } else {
                toast("缺少权限，无法使用相机/蓝牙。请在设置中开启。")
            }
        }

    private fun checkPermissions() {
        val missing = permissions.filter {
            ContextCompat.checkSelfPermission(this, it) != PackageManager.PERMISSION_GRANTED
        }
        if (missing.isEmpty()) {
            (application as HeartbeatApp).initWithPermissionsGranted()
            log("SDK 已初始化，权限已就绪。")
            setupOrchestrator()
        } else {
            permissionLauncher.launch(permissions.toTypedArray())
        }
    }

    /**
     * 把**单例里已经存在**的状态画到当前界面。
     *
     * 为什么必须有这个方法：编排者现在活过 Activity 重建（折叠屏折叠/展开、旋转都会重建），
     * 相机与手表连接都还在，但界面是全新的、文字全是布局里的默认值。
     * 不回填就会出现「明明连着却显示未连接」——这正是之前最误导人的假象，
     * 让人以为连接掉了、反复去点重连（还会撞上 `camera connect failed -214`）。
     */
    private fun restoreStateFromOrchestrator() {
        val snap = orchestrator.snapshotForUi()
        val restored = mutableListOf<String>()

        tvCapture.text = when {
            !snap.cameraConnected -> "相机: 未连接"
            snap.captureArmed -> "相机: ${modeVerb()}中 ▲（心率超阈值）"
            else -> "相机: 已连接（等待心率触发）"
        }
        if (snap.cameraConnected) restored += "相机"

        val name = snap.heartRateDeviceName
        when {
            name != null -> {
                tvDevice.text = "心率源: 已连接 $name"
                applyHeartRateConnectionState(connected = true, deviceName = name)
                restored += "心率设备"
            }

            snap.simulating -> {
                tvDevice.text = "心率源: 模拟心率"
                // 模拟心率在跑就代表「触发会话开着」→ 心率源区应展开，否则用户看不到停止入口
                hrSourceBody.visibility = View.VISIBLE
                btnToggleHrSource.text = "心率源 ▴"
                restored += "模拟心率"
            }
        }

        // 重建后心率读数已断（新界面没收到过值）→ 环与区间带复位到空态，
        // 等下一次心率上报（约 1 秒内）自己填回来。**不能保留旧数字**，
        // 那会让人以为还在实时跳动。
        currentHr = 0
        tvHr.text = "--"
        hrRing.clearHeartRate()
        zoneBand.clearHeartRate()
        updateZoneNow(0)
        syncThresholdVisuals()

        updateUploadButton(snap.uploadBusy)
        if (snap.uploadBusy) restored += "上传中"

        refreshMainSwitch()
        updatePills()

        if (restored.isNotEmpty()) {
            log("↻ 界面重建，已恢复既有状态：${restored.joinToString("、")}（无需重连）")
        }
    }

    private fun setupOrchestrator() {
        // 单例可能已经很忙了（旧 Activity 期间连的相机/手表都还在）→ 先把真实状态画到新界面上。
        restoreStateFromOrchestrator()
        orchestrator.setCallback(object : HeartbeatOrchestrator.Callback {
            override fun onHeartRate(hr: Int) {
                runOnUiThread {
                    currentHr = hr
                    // 环中央只显示数字，单位单独一行（60sp + 12sp 的层级差比「心率: 88 bpm」一行更清楚）
                    tvHr.text = hr.toString()
                    hrRing.setHeartRate(hr)
                    zoneBand.setHeartRate(hr)
                    updateZoneNow(hr)
                    updatePills()
                }
            }

            override fun onCameraTriggered(start: Boolean) {
                val verb = modeVerb()
                runOnUiThread {
                    tvCapture.text =
                        if (start) "相机: $verb ▲（心率超阈值)" else "相机: 已停止 $verb ▼（心率回落)"
                    log(if (start) ">> 触发$verb" else ">> 停止$verb")
                    // 会话列表：给「拍了几段」一个事实依据。
                    // 只由 App 自己的 armed 驱动，不依赖 isWorking()（见 doStopHeartRate 注释）。
                    addSessionRow(start)
                    refreshMainSwitch()
                }
            }

            override fun onCameraState(capturing: Boolean) {
                val verb = modeVerb()
                runOnUiThread {
                    tvCapture.text = if (capturing) "相机: 正在$verb" else "相机: 已停止"
                }
            }

            override fun onDeviceState(text: String) {
                runOnUiThread {
                    tvDevice.text = "心率源: $text"
                    refreshMainSwitch()
                    updatePills()
                }
            }

            override fun onHeartRateConnectionChanged(connected: Boolean, deviceName: String?) {
                runOnUiThread {
                    applyHeartRateConnectionState(connected, deviceName)
                    refreshMainSwitch()
                    updatePills()
                    refreshSelfCheckIfVisible()
                }
            }

            override fun onError(msg: String) {
                runOnUiThread { log(msg) }
            }
        })
    }

    /**
     * 往控制台的「本次会话」加一行触发记录。
     *
     * 存在的意义：`isWorking()` 在间隔拍照/延时的两次快门之间返回 false，
     * 界面无法用它判断「在不在拍」。会话列表给出的是 App 自己认定的事实
     * （每次 armed 的置位/复位各一行），用户据此能对上「刚才到底拍了几段」。
     */
    private fun addSessionRow(start: Boolean) {
        sessionEmpty.visibility = View.GONE
        val stamp = java.text.SimpleDateFormat("HH:mm:ss", java.util.Locale.CHINA)
            .format(java.util.Date())
        val text = if (start) "● REC 开始　$stamp" else "■ 结束　　$stamp"
        val colorRes = if (start) R.color.hr else R.color.text_secondary

        val row = TextView(this).apply {
            this.text = text
            textSize = 12f
            typeface = Typeface.MONOSPACE
            setTextColor(getColor(colorRes))
            setPadding(dpToPx(8), dpToPx(6), dpToPx(8), dpToPx(6))
            setBackgroundResource(if (start) R.drawable.bg_pill_hr else R.drawable.bg_row_normal)
        }
        // 最新的在最上面：演示时用户不用往下翻
        sessionList.addView(row, 0)
        while (sessionList.childCount > MAX_SESSION_ROWS) {
            sessionList.removeViewAt(sessionList.childCount - 1)
        }
    }

    /**
     * 挂上上传链路的回调：过程日志进日志区、当前阶段进状态行、忙时按钮切成「取消上传」。
     *
     * ⚠️ 必须用 runOnUiThread 包一层：下载/上传的回调来自 SDK 线程与 IO 线程，
     * 直接改 View 会抛 CalledFromWrongThreadException。
     */
    private fun configureUploader() {
        orchestrator.setUploadListener(object : ClipUploadManager.Listener {
            override fun onLog(msg: String) {
                runOnUiThread { log(msg) }
            }

            override fun onStage(text: String) {
                runOnUiThread { uploadStage.text = "上传状态：$text" }
            }

            override fun onBusy(busy: Boolean) {
                runOnUiThread { updateUploadButton(busy) }
            }

            override fun onMediaList(items: List<CameraController.MediaItem>) {
                runOnUiThread { renderMediaList(items) }
            }

            override fun onServerListRows(rows: List<String>) {
                runOnUiThread { renderServerList(rows) }
            }
        })
    }

    /**
     * 心率设备连接态变化 → 调整扫描区 UI。
     *
     * 需求：「手表和 APP 链接成功后，就不用再扫描别的设备了，也不用显示别的设备了。」
     *
     * 三种状态：
     *  - `deviceName != null`（已连上）：**停扫描、清空并隐藏设备列表、禁用扫描按钮**，
     *    按钮文案改成「已连接 xxx」。
     *  - `deviceName == null && connected == false`（未连/已断开/连接失败）：
     *    恢复扫描按钮可用，列表保持隐藏（等用户再点扫描才出现）。
     *  - `connected == false && deviceName == null` 也是「连接中」用的占位态，
     *    此时**不动列表**——因为连接过程需要几百毫秒到十几秒，中途清掉列表会让用户失去重选的机会。
     */
    private fun applyHeartRateConnectionState(connected: Boolean, deviceName: String?) {
        val btnScan = findViewById<Button>(R.id.btnScanBle)

        if (connected && deviceName != null) {
            // ① 已连上：停扫描 + 清空并隐藏列表 + 锁住扫描按钮
            scanAbandoned = true
            orchestrator.stopScan()
            renderHandler.removeCallbacksAndMessages(null)
            renderScheduled = false
            hrDevices.clear()
            rowViews.clear()
            lastRenderSignature = null
            deviceList.removeAllViews()
            deviceList.visibility = android.view.View.GONE
            scanHint.visibility = android.view.View.GONE
            btnScan.isEnabled = false
            btnScan.alpha = 0.5f
            btnScan.text = "已连接 $deviceName（无需再扫描）"
        } else {
            // ② 未连/断开/失败：恢复可扫描
            scanAbandoned = false
            btnScan.isEnabled = true
            btnScan.alpha = 1f
            btnScan.text = "扫描心率设备（点选列表里的 FR255）"
            deviceList.visibility = android.view.View.GONE
            scanHint.visibility = android.view.View.GONE
        }
    }

    /**
     * 扫描 BLE 心率设备。
     *
     * ⚠️ 这里**不自动连接**任何设备。
     * 早期版本是「连第一个发现的」，实测会连到耳机/电视等无关设备，导致心率永远收不到。
     * 现在把扫到的设备做成列表，由用户点选要连的那台。
     *
     * 列表排序依据（越靠前越可能是你的心率设备）：
     *  1. **广播包里带 0x180D** —— 最硬证据，基本可以直接点；
     *  2. 名字含 Garmin / Polar / HR 等关键字；
     *  3. 已与手机系统配对过（Garmin 常常配过，点它最稳）；
     *  4. 信号强度（RSSI 越强说明离你越近，FR255 就戴在你手上，通常是最强的那台）。
     */
    private fun scanForHeartRateDevices() {
        val manager = getSystemService(BLUETOOTH_SERVICE) as BluetoothManager
        val adapter = manager.adapter
        if (adapter == null || !adapter.isEnabled) {
            toast("请先开启蓝牙")
            return
        }
        hrDevices.clear()
        rowViews.clear()
        lastRenderSignature = null
        deviceList.removeAllViews()
        deviceList.visibility = android.view.View.VISIBLE
        scanAbandoned = false
        tvDevice.text = "心率源: 扫描中..."
        showScanHint("正在扫描…（10 秒）出现设备后点它即可连接。")

        // 已配对的设备先列出来：Garmin 往往已与手机配对过，点它最稳。
        // 注意：已配对设备拿名字只能走 device.name（binder），但这类设备通常只有几台，
        // 且只在扫描开始时读一次，不影响性能。
        val bonded = runCatching { adapter.bondedDevices }.getOrNull().orEmpty()
        bonded
            .filter { looksLikeHeartRate(runCatching { it.name }.getOrNull()) }
            .forEach { dev ->
                val address = dev.address ?: return@forEach
                hrDevices[address] = ScannedRow(
                    device = dev,
                    name = runCatching { dev.name }.getOrNull() ?: "未知设备",
                    rssi = 0,
                    advertisesHr = false,
                    bonded = true,
                )
            }

        orchestrator.startScan(
            onDevice = { found ->
                // 用户已点选设备、放弃本轮扫描 → 迟到的回调直接丢弃，不要重画列表
                if (scanAbandoned) return@startScan
                found.forEach { s ->
                    val address = s.device.address ?: return@forEach
                    val prev = hrDevices[address]
                    // 同一台设备可能被多次回调，保留「证据更强 / 信号更强」的那条
                    if (prev == null ||
                        (!prev.advertisesHr && s.advertisesHeartRate) ||
                        (prev.advertisesHr == s.advertisesHeartRate && s.rssi > prev.rssi)
                    ) {
                        hrDevices[address] = ScannedRow(
                            device = s.device,
                            // ⚠️ 用广播包里的名字（零成本），绝不在渲染路径调 device.name
                            name = s.advertisedName ?: prev?.name ?: "未知设备",
                            rssi = s.rssi,
                            advertisesHr = s.advertisesHeartRate,
                            bonded = prev?.bonded ?: bonded.any { it.address == address },
                        )
                    }
                }
                // ⚠️ 必须节流：本 lambda 每秒会被调用几百次，直接重绘会 ANR
                scheduleRender()
            },
            onDone = {
                // 主动放弃的扫描不回调 onDone（见 HeartRateScanner.stop 注释），
                // 但这里再兜一层，防止将来改动后漏出「扫描结束」提示
                if (scanAbandoned) return@startScan
                renderDeviceListNow()
                if (hrDevices.isEmpty()) {
                    log(
                        "扫描结束，未发现任何 BLE 设备。请确认：\n" +
                            "  · 手机蓝牙已开\n" +
                            "  · FR255 已开「心率广播」（手表屏幕上要有心率图标）\n" +
                            "  · 手表没有同时连在别的手机上（一台心率广播只能被一台手机占用）"
                    )
                } else {
                    val top = sortedRows().first()
                    val topName = runCatching { top.device.name }.getOrNull() ?: top.device.address
                    log(
                        "扫描结束，共 ${hrDevices.size} 台设备。\n" +
                            "最可能的是列表第 1 行「$topName」——直接点它。"
                    )
                }
            }
        )
    }

    /** 扫描到的一行。 */
    private data class ScannedRow(
        val device: BluetoothDevice,
        /** 设备名。**缓存下来**，渲染时绝不再调 device.name（那是 binder 调用）。 */
        val name: String,
        val rssi: Int,
        val advertisesHr: Boolean,
        val bonded: Boolean,
    )

    /**
     * 按「证据强度」排序：带 0x180D > 名字像 > 已配对 > 信号强。
     * 全部使用缓存字段，不做任何 binder 调用。
     */
    private fun sortedRows(): List<ScannedRow> =
        hrDevices.values.sortedWith(
            compareByDescending<ScannedRow> { it.advertisesHr }
                .thenByDescending { looksLikeHeartRate(it.name) }
                .thenByDescending { it.bonded }
                .thenByDescending { it.rssi }
        )

    /**
     * 请求重绘（可高频调用，内部会合并）。
     *
     * ⚠️ 为什么必须节流：BLE 扫描回调在主线程触发，LOW_LATENCY 下每秒几百次。
     * 每来一次就整表重建 → 主线程压死 → ANR。见 [renderDeviceListNow]。
     */
    private fun scheduleRender() {
        if (renderScheduled) return
        renderScheduled = true
        renderHandler.postDelayed({
            renderScheduled = false
            renderDeviceListNow()
        }, RENDER_THROTTLE_MS)
    }

    /** 真正重绘：先比对签名，内容没变就直接返回（绝大多数回调走到这里就结束）。 */
    private fun renderDeviceListNow() {
        if (scanAbandoned) return
        val rows = sortedRows()

        // 签名只含「影响排序/标记」的信息（地址 + 标记 + 10dBm 档位的信号），
        // 不含完整 RSSI —— 否则 RSSI 每变 1 dBm 就要重绘一次，白白浪费主线程。
        val sig = rows.joinToString("|") {
            "${it.device.address}:${it.advertisesHr}:${it.bonded}:${it.rssi / 10}"
        }
        if (sig == lastRenderSignature) return
        lastRenderSignature = sig

        deviceList.removeAllViews()
        rows.forEachIndexed { index, row ->
            deviceList.addView(rowView(row, isTop = index == 0))
        }
    }

    /** 按名字猜是不是心率设备（仅用于排序，不作最终判定）。 */
    private fun looksLikeHeartRate(name: String?): Boolean {
        val n = name?.lowercase() ?: return false
        return HR_NAME_HINTS.any { n.contains(it) }
    }

    /** 取（或创建）一行设备视图。已存在的行只更新文字，不重复 new TextView。 */
    private fun rowView(row: ScannedRow, isTop: Boolean): LinearLayout {
        val address = row.device.address ?: "??"
        val cached = rowViews[address]
        if (cached != null) {
            bindRow(cached, row, isTop)
            return cached
        }
        // 一行 = 上面一行小字标记(tag) + 下面设备名/地址(body)
        val container = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        val tag = TextView(this).apply { textSize = 12f; setPadding(24, 16, 24, 0) }
        val body = TextView(this).apply {
            textSize = 15f
            setPadding(24, 2, 24, 18)
            isClickable = true
            isFocusable = true
        }
        container.addView(tag)
        container.addView(body)
        bindRow(container, row, isTop)
        rowViews[address] = container
        return container
    }

    /** 把数据写进某一行视图（含「点击连接」逻辑）。 */
    private fun bindRow(container: LinearLayout, row: ScannedRow, isTop: Boolean) {
        val dev = row.device
        val name = row.name
        val address = dev.address ?: "??"
        val likely = looksLikeHeartRate(name) || row.advertisesHr

        val tag = container.getChildAt(0) as TextView
        val body = container.getChildAt(1) as TextView

        tag.text = buildString {
            if (row.advertisesHr) append("✔ 广播含心率服务  ")
            if (isTop && !row.advertisesHr) append("★ 最可能  ")
            if (row.bonded) append("· 已配对  ")
            append("信号 ").append(row.rssi).append(" dBm")
        }
        tag.setTextColor(
            ContextCompat.getColor(
                this,
                if (row.advertisesHr) R.color.heart_rate else R.color.text_secondary
            )
        )

        body.text = "$name\n$address"
        body.setTextColor(
            ContextCompat.getColor(
                this,
                if (likely) R.color.heart_rate else R.color.text_primary
            )
        )
        body.setOnClickListener {
            tvDevice.text = "心率源: 连接中 $name ..."
            log("连接 $name ($address) ...")
            // 点了就停止扫描：选定了就不用再扫，也避免列表继续刷出无关设备干扰视线。
            // 真正的收起（隐藏列表 + 锁按钮）在 applyHeartRateConnectionState() 里做——
            // 那是等 GATT 真的连上才触发，连接失败时列表还能让用户重选。
            scanAbandoned = true
            orchestrator.stopScan()
            orchestrator.attachHeartRateDevice(dev)
        }
    }

    /** 当前模式的动词，用于状态文案（「正在录像」/「正在间隔拍照」）。 */
    private fun modeVerb(): String =
        if (orchestrator.currentSettings().captureMode.usesInterval) "间隔拍照" else "录像"

    private fun showScanHint(text: String) {
        scanHint.text = text
        scanHint.visibility = android.view.View.VISIBLE
    }

    // ---- 工具 ----

    /**
     * 写一行界面日志。
     *
     * 两个必须保留的行为：
     *  1. **同时打到 logcat**（标签 [TAG]）。界面上的日志只有手机屏幕能看到，
     *     adb / 抓屏都拿不到，真机排查时等于「什么都看不见」。
     *     `adb logcat -s HeartbeatUI` 就能把全部界面日志原样抓下来。
     *  2. **分级着色**。单一颜色的长日志在排障时几乎没法扫读；
     *     按前缀/关键词判级别后，一眼就能定位「哪几行是失败」。
     */
    private fun log(msg: String) {
        android.util.Log.i(TAG, msg)
        runOnUiThread {
            // 淘汰最旧的行。存 (文本, 颜色) 而不是直接拼 Spannable，
            // 就是为了能整行淘汰 —— 对 SpannableStringBuilder 做头部裁剪会把跨行 span 剪坏。
            if (logLines.size >= MAX_LOG_LINES) logLines.removeFirst()
            logLines.addLast(msg to colorForLog(msg))

            val sb = SpannableStringBuilder()
            logLines.forEach { (m, c) ->
                val start = sb.length
                sb.append("• ").append(m).append('\n')
                sb.setSpan(
                    ForegroundColorSpan(getColor(c)),
                    start,
                    sb.length,
                    Spannable.SPAN_EXCLUSIVE_EXCLUSIVE,
                )
            }
            tvLog.text = sb
            logScroll.post { logScroll.fullScroll(View.FOCUS_DOWN) }
        }
    }

    /**
     * 按内容判级别（日志没有结构化级别，用前缀约定）。
     *
     * ⚠️ 判定顺序有讲究：「失败」要先于「已连接」判 ——
     * 「❌ 下载失败」不该因为含「连接」字样被染成成功色。
     */
    private fun colorForLog(msg: String): Int = when {
        msg.startsWith("⚠") -> R.color.term_warn
        msg.startsWith("❌") || msg.contains("失败") || msg.contains("异常") -> R.color.term_err
        msg.startsWith("✅") || msg.startsWith("✓") -> R.color.term_ok
        msg.startsWith(">>") || msg.startsWith("↻") || msg.startsWith("👆") -> R.color.term_hl
        else -> R.color.term_info
    }

    private fun toast(msg: String) {
        Toast.makeText(this, msg, Toast.LENGTH_SHORT).show()
    }

    override fun onDestroy() {
        super.onDestroy()
        // onCreate 若中途失败，orchestrator 可能未初始化，这里做保护，避免二次崩溃掩盖真实原因
        if (!::orchestrator.isInitialized) return

        runCatching {
            if (isFinishing) {
                // 用户真的退出了（返回键 / 划掉任务）→ 拆掉整条链路，释放相机会话与 GATT
                log("Activity 结束（isFinishing）→ 释放相机与心率连接")
                orchestrator.cleanup()
            } else {
                // 只是**配置变更**（折叠/展开、旋转、深色模式…）导致的重建：
                // 只摘掉 UI 回调防泄漏，相机/手表连接原样留着，新 Activity 起来无缝续用。
                // ⚠️ 这里绝不能 cleanup() —— 那会「把刚连好的相机拆掉」。
                orchestrator.detachUi()
            }
        }
    }

    private companion object {
        /** logcat 标签：`adb logcat -s HeartbeatUI` 即可抓全部界面日志。 */
        const val TAG = "HeartbeatUI"

        /**
         * 扫描列表的最小重绘间隔（毫秒）。
         *
         * BLE 扫描回调在主线程高频触发（LOW_LATENCY 模式下每秒可达数百次），
         * 全部直接重绘会把主线程压死 → MIUI 弹「心跳控制相机无响应」（ANR）。
         * 120ms ≈ 8fps，肉眼完全够流畅，主线程压力降到 1/50 以下。
         */
        const val RENDER_THROTTLE_MS = 120L

        /**
         * 设备名里的心率关键字，用于把可疑设备排到前面。
         * Garmin 手表广播心率时，广播名通常仍是手表型号（如 "Forerunner 255"），
         * 不一定含 "HR"，所以 "forerunner" / "garmin" 是这里最关键的项。
         */
        val HR_NAME_HINTS = listOf(
            "garmin", "forerunner", "fenix", "venu", "vivoactive", "instinct",
            "polar", "wahoo", "tickr", "coospo", "magene", "hr", "heart", "hrm",
        )

        /** AppBar 标题（与底部 Tab 一一对应）。 */
        val TAB_TITLES = listOf("控制台", "素材", "设置", "诊断")

        /**
         * 终端最多保留多少行。
         *
         * 每次 log 都要重排一次富文本，行数无上限的话，
         * 长时间运行后单次 log 的开销会随日志长度线性增长（最终卡主线程）。
         */
        const val MAX_LOG_LINES = 300

        /** 控制台会话列表最多保留几行。 */
        const val MAX_SESSION_ROWS = 8

        /**
         * 间隔的快捷值。取自相机固件**确实支持**的档位
         * （见 `go_ultra.json` 的 `lapse_time`）。
         *
         * 点这些值走的是固件自己的节拍（最准）；填其它值不会失败，
         * 只是会退化成 App 侧定时器实现（见 docs/素材上传.md 与 v10 记忆）。
         */
        val QUICK_INTERVALS = listOf(1, 3, 5, 10, 30, 60)

        /** 阈值滑块的端点（与 [AppSettings.MIN_HR] / [AppSettings.MAX_HR] 保持一致）。 */
        const val MIN_HR_F = 30f
        const val MAX_HR_F = 250f

        /** 深链参数：`--ei tab <0..3>` 直接落到对应 Tab（0=控制台 … 3=诊断）。 */
        const val EXTRA_TAB = "tab"

        /** Tab 顺序必须与 [TAB_TITLES] 一致。 */
        val TAB_IDS = listOf(R.id.tab_console, R.id.tab_media, R.id.tab_settings, R.id.tab_diag)
    }

    /**
     * `launchMode="singleTop"` 时再次 `am start` 走这里而不是 onCreate，
     * 深链要在这里同样生效，否则第二次 `am start --ei tab 3` 会没反应。
     */
    override fun onNewIntent(intent: android.content.Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        val requested = intent.getIntExtra(EXTRA_TAB, -1)
        if (requested in TAB_IDS.indices) {
            navBar.selectedItemId = TAB_IDS[requested]
        }
    }
}
