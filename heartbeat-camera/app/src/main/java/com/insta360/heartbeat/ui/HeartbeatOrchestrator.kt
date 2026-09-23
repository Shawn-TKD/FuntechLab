package com.insta360.heartbeat.ui

import android.bluetooth.BluetoothDevice
import android.content.Context
import com.insta360.heartbeat.ble.HeartRateMonitor
import com.insta360.heartbeat.ble.HeartRateScanner
import com.insta360.heartbeat.ble.SimulatedHeartRateSource
import com.insta360.heartbeat.camera.CameraController
import com.insta360.heartbeat.camera.CaptureMode
import com.insta360.heartbeat.settings.AppSettings
import com.insta360.heartbeat.threshold.ThresholdEngine
import com.insta360.heartbeat.upload.ClipUploadManager
import timber.log.Timber

/**
 * 编排者：把「BLE 心率 / 模拟心率」+「阈值引擎」+「相机」串成完整链路。
 *
 * 数据流：
 *   心率源(onHeartRate) → ThresholdEngine.onHeartRate → 若需变化 → CameraController.start/stop
 *
 * 提供两种心率源：
 *   - [attachGarminDevice]：真实 BLE 心率设备（Garmin FR255 等）
 *   - [toggleSimulation]：模拟心率（无设备时跑通链路用）
 *
 * ## ⚠️ 为什么是**进程级单例**（真机踩过的大坑）
 *
 * 本对象持有三类「长连接」：SDK 的相机会话、BLE GATT 连接、模拟心率协程。
 * 它们都不是「一个界面」的东西，而是「一次会话」的东西。
 *
 * 早期版本在 `MainActivity.onCreate` 里 `HeartbeatOrchestrator(this)`、在 `onDestroy` 里
 * `cleanup()` —— 结果是**任何一次 Activity 重建都会把整条链路拆掉**。
 * 折叠屏上折叠/展开、旋转、深色模式切换、字体缩放全都触发重建，
 * 于是：「刚连上的相机莫名其妙变未连接」「重连报 `camera connect failed -214`」
 * 「日志里反复出现已取消上传」。
 *
 * 现在：Activity 只通过 [setCallback] / [setUploadListener] **挂/摘** UI 回调，
 * 编排者本身由 [get] 提供、跟着进程走；只有用户主动点「停止/断开」或 Activity
 * 真正 `isFinishing` 时才 [cleanup]。
 */
class HeartbeatOrchestrator(private val context: Context) {

    interface Callback {
        /** 实时心率（无论来自真设备还是模拟）。 */
        fun onHeartRate(hr: Int)

        /** 触发相机开关。start=true 表示开录，false 表示暂停。 */
        fun onCameraTriggered(start: Boolean)

        fun onCameraState(capturing: Boolean)

        /** 心率源连接状态变化，用于更新「心率源: xxx」那一行。 */
        fun onDeviceState(text: String)

        /**
         * 心率设备是否已连上。
         *
         * 单独开一个回调而不是让 UI 去解析 [onDeviceState] 的文案——
         * 字符串匹配太脆（改一个字 UI 就失灵），而且 UI 需要据此**收起设备列表、
         * 禁用扫描按钮**，这是个明确的状态而不是一句提示。
         *
         * @param connected true=已连上；false=未连/已断开/连接失败（UI 应恢复可扫描状态）
         * @param deviceName 已连接设备名，未连接时为 null
         */
        fun onHeartRateConnectionChanged(connected: Boolean, deviceName: String?)

        fun onError(msg: String)
    }

    private val monitor = HeartRateMonitor(context)
    private val scanner = HeartRateScanner(context)
    private val simulated = SimulatedHeartRateSource()
    private val engine = ThresholdEngine(
        onThreshold = AppSettings.DEFAULT_ON,
        offThreshold = AppSettings.DEFAULT_OFF,
    )
    private val camera = CameraController(context)

    /** 「录完 → 下载 LRV → 转 MP4 → 上传服务器」这条支线的编排者。 */
    private val uploader = ClipUploadManager(context, camera)

    /** 持久化的用户设置（阈值 / 间隔 / 模式 / 上传地址）。 */
    private val settings = AppSettings(context)

    private var callback: Callback? = null

    init {
        // 冷启动时把上次保存的设置灌进引擎与相机控制器
        applySettingsToEngine()
    }

    // ---- 设置 ----

    /** 把持久化设置同步到阈值引擎和相机控制器。改完设置后调用。 */
    fun applySettingsToEngine() {
        engine.onThreshold = settings.onThreshold
        engine.offThreshold = settings.offThreshold
        // 模拟心率也必须跟随用户设置，否则开拍阈值调到 120 以上时，
        // 旧曲线最高只有 120，模拟源永远无法触发 START。
        simulated.setThresholds(engine.onThreshold, engine.offThreshold)
        camera.captureMode = settings.captureMode
        camera.intervalSeconds = settings.intervalSeconds
    }

    /**
     * 保存设置（带钳制），并把生效值回传 UI 与内部组件。
     *
     * ## 改完设置「立即按新参数跑」，不用等心率回落再上来一次
     *
     * 做法：若当时正在拍摄 → 先停相机；然后**一律复位引擎**。
     * 复位后状态是 PAUSED，而心率此刻通常仍高于（新的）开拍阈值，
     * 于是**下一次心率上报（最迟 1 秒后）就会用新参数自动重新开始**。
     *
     * 为什么必须停相机：参数是下发到相机的，改了模式/间隔却不重开，相机会继续按旧参数拍。
     * 早期版本还因此留下隐患 —— 引擎复位而相机还在录，之后心率回落时引擎不产生 STOP，
     * 相机就永远停不下来。
     */
    fun saveSettings(
        on: Int,
        off: Int,
        interval: Int,
        mode: CaptureMode,
    ): AppSettings.Saved {
        val wasRecording = engine.state == ThresholdEngine.State.REC
        val saved = settings.save(on, off, interval, mode)

        // 先停相机（此时 camera.captureMode 还是旧值，日志里的动词才对得上）
        if (wasRecording) {
            camera.stopRecord()
            callback?.onCameraTriggered(false)
        }
        // 再把新设置灌进引擎与相机（含新的模式/间隔）
        applySettingsToEngine()
        // 复位引擎；心率若仍高于新阈值，下一拍会以新参数重新开拍
        engine.reset()
        return saved
    }

    /** 读当前持久化设置，供 UI 初始化输入框。 */
    fun currentSettings() = settings

    /** 当前生效的开录 / 暂停阈值，供 UI 展示。 */
    fun currentThresholds(): Pair<Int, Int> = engine.onThreshold to engine.offThreshold

    // ---- 素材上传到服务器 ----

    /** 当前保存的上传地址，供 UI 回填输入框。 */
    fun currentUploadUrl(): String = settings.uploadUrl

    /** 保存上传地址（点上传按钮时顺手存，不额外加一个保存按钮）。 */
    fun saveUploadUrl(url: String) {
        settings.uploadUrl = url
    }

    /** 给上传链路挂回调（过程日志 / 状态行 / 按钮置灰）。传 null 表示 UI 已销毁。 */
    fun setUploadListener(l: ClipUploadManager.Listener?) {
        uploader.setListener(l)
    }

    /**
     * 一键把**最新一段**素材传上服务器：列素材 → 下载 .lrv → 转 MP4 → 上传。
     * 内部全程走日志，失败原因会写清楚（见 [ClipUploadManager]）。
     */
    fun uploadLatestClip(url: String) {
        saveUploadUrl(url)
        uploader.start(url)
    }

    fun cancelUpload() = uploader.cancel()

    /** 只刷新素材列表（不下载、不上传）。列表通过 [setUploadListener] 的 onMediaList 回填给界面。 */
    fun refreshMediaList() = uploader.refreshMediaList()

    /**
     * 查看**服务器上已有**的素材（按文件夹列出）。
     * 结果通过 [setUploadListener] 的 onServerListRows 回填给界面。
     */
    fun refreshServerList(url: String) {
        saveUploadUrl(url)
        uploader.refreshServerList(url)
    }

    /** 上传用户在上方列表里**点选**的那一条素材。 */
    fun uploadMediaItem(item: CameraController.MediaItem, url: String) {
        saveUploadUrl(url)
        uploader.uploadItem(item, url)
    }

    fun isUploading(): Boolean = uploader.isBusy()

    // ---- 对外接口 ----

    /**
     * 当前链路状态快照，**专供 Activity 重建后回填界面**。
     *
     * 为什么需要：编排者是进程级单例，折叠/旋转后连接都还在，
     * 但界面是全新的、所有状态文字都是默认值。不回填的话，
     * 用户会看到「明明连着却显示未连接」，这正是之前最误导人的假象。
     */
    data class UiSnapshot(
        val cameraConnected: Boolean,
        /** 心率触发的拍摄会话是否开着（注意不等于「相机此刻正在录」）。 */
        val captureArmed: Boolean,
        /** 已连上的 BLE 心率设备名；未连为 null。 */
        val heartRateDeviceName: String?,
        /** 是否正在跑模拟心率。 */
        val simulating: Boolean,
        val uploadBusy: Boolean,
    )

    fun snapshotForUi(): UiSnapshot = UiSnapshot(
        cameraConnected = camera.isConnected(),
        captureArmed = camera.isArmed(),
        heartRateDeviceName = if (monitor.isConnected()) monitor.currentDeviceName() else null,
        simulating = simulated.isRunning(),
        uploadBusy = uploader.isBusy(),
    )

    /**
     * 挂 UI 回调。传 null 用于 Activity 销毁时**解绑**。
     *
     * ⚠️ 解绑是必须的：编排者是进程级单例，若一直握着 Activity 的匿名内部类，
     * 每次折叠/旋转都会泄漏一个 Activity（它引着整棵 View 树）。
     */
    fun setCallback(cb: Callback?) {
        callback = cb
    }

    /** 扫描标准 BLE 心率设备（Garmin FR255 开启心率广播后可见）。 */
    fun startScan(onDevice: (List<ScannedDevice>) -> Unit, onDone: () -> Unit) {
        scanner.start(listener = object : HeartRateScanner.Callback {
            override fun onDeviceFound(
                device: BluetoothDevice,
                rssi: Int,
                advertisesHeartRate: Boolean,
                advertisedName: String?,
            ) {
                onDevice(listOf(ScannedDevice(device, rssi, advertisesHeartRate, advertisedName)))
            }

            override fun onScanFinished() {
                onDone()
            }

            override fun onScanError(msg: String) {
                callback?.onError(msg)
                onDone()
            }
        })
    }

    /** 扫描到的一台设备。 */
    data class ScannedDevice(
        val device: BluetoothDevice,
        val rssi: Int,
        /** 广播包里是否带 0x180D —— 比名字更可靠的判据。 */
        val advertisesHeartRate: Boolean,
        /**
         * 广播包里自带的名字（可能为 null）。
         * ⚠️ UI 显示名字必须用这个，**不要**去调 `device.name`（binder 调用，高频渲染下会 ANR）。
         */
        val advertisedName: String?,
    )

    fun stopScan() = scanner.stop()

    /** 连接选中设备并订阅心率。 */
    fun attachHeartRateDevice(device: BluetoothDevice) {
        simulated.stop()
        val name = runCatching { device.name }.getOrNull() ?: device.address
        callback?.onDeviceState("连接中…")
        // 先置为「连接中」：此时既不能算已连上（要收起列表），也不能算失败（要保留列表）。
        // UI 用 null 表示「连接中，先别动列表」。
        callback?.onHeartRateConnectionChanged(false, null)
        monitor.connect(device, object : HeartRateMonitor.Listener {
            override fun onHeartRate(hr: Int) {
                feed(hr)
            }

            override fun onConnected(device: BluetoothDevice) {
                // 注意：连上 ≠ 能收到心率。这里只是 GATT 通了，还要等服务发现 + 订阅 CCCD。
                val n = runCatching { device.name }.getOrNull() ?: device.address
                callback?.onDeviceState("已连接 $n（正在订阅心率…）")
                callback?.onError("已连接 $n，正在发现心率服务…")
                // GATT 已通 = 设备选择完成，可以收起列表了
                callback?.onHeartRateConnectionChanged(true, n)
            }

            override fun onReady(device: BluetoothDevice) {
                // 订阅真正生效，从这一刻起才会有心率数据
                val n = runCatching { device.name }.getOrNull() ?: device.address
                callback?.onDeviceState("已连接 $n（心率订阅中）")
                callback?.onError("心率订阅成功，等几秒就会出数")
            }

            override fun onDisconnected(device: BluetoothDevice) {
                callback?.onDeviceState("已断开")
                callback?.onError("心率设备断开")
                // 断开了就恢复可扫描状态，让用户能重新选设备
                callback?.onHeartRateConnectionChanged(false, null)
            }

            override fun onError(msg: String) {
                callback?.onDeviceState("连接异常")
                callback?.onError(msg)
                // 连接失败也要恢复可扫描，否则用户被卡死没法重试
                callback?.onHeartRateConnectionChanged(false, null)
            }
        })
    }

    /** 切换模拟心率（无 Garmin 时验证链路）。 */
    fun toggleSimulation(on: Boolean) {
        if (on) {
            // 模拟源会顶掉真实 BLE 连接（close() 里 listener 一并清空，所以不会回调 onDisconnected），
            // 这里要主动把连接态置回「未连接」，否则 UI 会一直以为手表还连着。
            monitor.close()
            callback?.onHeartRateConnectionChanged(false, null)
            callback?.onDeviceState("模拟心率")
            simulated.start(listener = object : SimulatedHeartRateSource.Listener {
                override fun onSimulatedHeartRate(hr: Int) {
                    feed(hr)
                }
            })
        } else {
            simulated.stop()
            callback?.onDeviceState("未连接")
            callback?.onHeartRateConnectionChanged(false, null)
        }
    }

    // ---- 相机 ----

    /** 连接 GO Ultra（WiFi 直连）。加载后把相机状态透传给 UI。 */
    fun connectCamera() {
        camera.connectToWifiCamera(object : CameraController.Listener {
            override fun onConnectionChanged(connected: Boolean) {
                callback?.onError(if (connected) "相机已连接" else "相机未连接")
            }

            override fun onCaptureStateChanged(capturing: Boolean) {
                callback?.onCameraState(capturing)
            }

            override fun onCaptureFinished(filePaths: List<String>) {
                callback?.onError("拍摄完成，共 ${filePaths.size} 个文件")
            }

            override fun onStartFailed(reason: String) {
                // 异步阶段失败（模式切换被拒 / startCapture 被拒）——同步阶段已经返回过 true，
                // 此时引擎还停在 REC。必须回滚，否则心率一直高时再也不会重试。
                engine.reset()
                callback?.onCameraTriggered(false)
            }

            override fun onError(msg: String) {
                callback?.onError(msg)
            }
        })
    }

    fun activateCamera(appId: String, secretKey: String) =
        camera.activate(appId, secretKey)

    /**
     * 「停止」：停掉**心率侧**的一切 —— 模拟心率、BLE 心率带、扫描，
     * 并结束心率触发的拍摄会话（让相机停止录制）。
     *
     * ⚠️ **刻意不动相机连接。** 一次演示里最贵的是相机 WiFi 重连
     * （要切系统网络、等 SDK 握手），而心率源随时能重启。
     * 拆成两个独立动作后，就能在「相机一直连着」的前提下反复重启心率侧做多轮演示。
     */
    fun stopHeartRate() {
        simulated.stop()
        monitor.close()
        scanner.stop()

        // 心率没了，触发会话必须一起结束 —— 否则相机会脱离心率控制继续录，
        // 变成「没人能停的录制」。stopRecord() 只认 armed，与 capturing 无关。
        camera.stopRecord()
        // 引擎可能停在 REC 边沿态，必须复位，否则下次启动后只要心率一直高就再也不发 START。
        engine.reset()

        // 主动停止不会走 monitor 的 onDisconnected（close() 已清掉 listener），
        // 必须显式把连接态推给 UI，否则按钮会一直卡在「已连接 xxx（无需再扫描）」。
        callback?.onHeartRateConnectionChanged(false, null)
        callback?.onDeviceState("未连接")
    }

    /**
     * 「断开」：断开与相机的连接，**不动心率源**（模拟心率/手表继续跑）。
     *
     * 相机断开后心率再越过阈值只会触发失败 —— 由 CameraController.startRecord()
     * 返回 false + 引擎回滚兜底（v10 的修复），不会卡死在 REC 状态。
     */
    fun disconnectCamera() {
        uploader.cancel()
        camera.release()
    }

    /** 彻底收摊（Activity 真正退出时调用）：心率侧 + 相机侧全断。 */
    fun cleanup() {
        stopHeartRate()
        disconnectCamera()
        uploader.release()
        callback = null
        uploader.setListener(null)
    }

    /**
     * 只解绑 UI 回调，**不动任何连接**。
     *
     * 用途：Activity 因配置变更（折叠/展开、旋转、深色模式…）被重建时，
     * 旧的 Activity 实例即将销毁 —— 必须把回调摘掉防止泄漏，
     * 但相机/手表/模拟心率要原样留着，新 Activity 起来后重新挂上回调即可无缝续用。
     */
    fun detachUi() {
        callback = null
        uploader.setListener(null)
    }

    companion object {
        @Volatile
        private var instance: HeartbeatOrchestrator? = null

        /**
         * 取进程级单例。
         *
         * ⚠️ 内部一律用 `applicationContext`：编排者活得比 Activity 久，
         * 握着 Activity context 就是泄漏。
         */
        fun get(context: Context): HeartbeatOrchestrator =
            instance ?: synchronized(this) {
                instance ?: HeartbeatOrchestrator(context.applicationContext).also { instance = it }
            }
    }

    // ---- 内部核心 ----

    private fun feed(hr: Int) {
        callback?.onHeartRate(hr)
        val action = engine.onHeartRate(hr)
        Timber.d("hr=$hr state=${engine.state} action=$action mode=${settings.captureMode}")
        when (action) {
            ThresholdEngine.Action.START -> {
                // ⚠️ 顺序关键：**先问相机能不能拍，只有相机接受了才通知 UI**。
                // 早期版本反过来（先告诉 UI「已开拍」再调相机），相机没连上时界面照样写「已开拍」，
                // 属于「界面说假话」，真机排查时被它误导过（日志铁证：先 ">> 触发录像"、36ms 后
                // 才打 "⚠ 无法开拍：相机未连接"）。
                if (camera.startRecord()) {
                    callback?.onCameraTriggered(true)
                } else {
                    // 相机没接受 → **必须回滚引擎状态**。
                    // 不回滚的话，只要心率一直高于阈值，引擎会永久停在 REC 且返回 NONE，
                    // 再也不会重发 START —— 表现就是「心率明明超标，相机却再也不响应」。
                    // 回滚后下一次心率上报（1 秒后）会继续尝试，相机一连上就自动开拍。
                    engine.reset()
                }
            }

            ThresholdEngine.Action.STOP -> {
                // 先停相机（内部会按 armed 判断），再通知界面
                camera.stopRecord()
                callback?.onCameraTriggered(false)
            }

            ThresholdEngine.Action.NONE -> {}
        }
    }
}
