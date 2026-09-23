package com.insta360.heartbeat.camera

import android.content.Context
import android.net.ConnectivityManager
import android.net.LinkProperties
import android.net.NetworkCapabilities
import android.os.SystemClock
import com.arashivision.sdk.camera.api.CameraDevice
import com.arashivision.sdk.camera.api.param.CameraParam
import com.arashivision.sdk.camera.api.param.listener.CaptureStatusListener
import com.arashivision.sdk.camera.core.model.ConnectType
import com.arashivision.sdk.camera.core.model.FunctionMode
import com.arashivision.sdk.camera.core.model.capture.CameraCaptureStatus
import com.arashivision.sdk.camera.core.model.file.MediaFileType
import java.io.File
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import timber.log.Timber

/**
 * Insta360 GO Ultra 控制封装。
 *
 * ## 两个「在拍」状态，千万别混
 *
 * | 字段 | 含义 | 谁设置 |
 * |---|---|---|
 * | [armed] | **心率触发的拍摄会话开着**（= 心率高于开拍阈值那段时间） | 本类主动设置 |
 * | [capturing] | **相机自报正在拍摄** | 相机状态回调 |
 *
 * ⚠️ 官方 demo（`CameraCaptureViewModel` 注释）明确：**间隔拍照/星轨/延时在两次快门之间
 * `isWorking()` 会返回 false，`capturing` 也会跟着抖**。所以「这次采集到底开没开」**只认 [armed]**，
 * `capturing` 仅用于界面展示与「别把命令堆在一起」的防重入判断。
 *
 * 早期版本只有 `capturing` 一个标志，导致两个真机问题：
 *  1. 间隔拍照两张之间 `capturing=false`，被误判成「没在拍」；
 *  2. 停止时若 `capturing=false` 又 `isWorking()=false` 就直接跳过停止命令，
 *     于是「界面以为停了、相机还在按节拍拍」。
 *
 * ## 间隔拍照的两种实现（都支持任意秒数）
 *
 * GO Ultra 固件的 `lapse_time` 只接受**离散值**（`go_ultra.json` 实证：`[0,1,3,5,10,30,60,120]` 秒）。
 * 用户想填 7 秒呢？两条路二选一：
 *  - **3a 固件原生**：填的值落在合法值里 → 切 `PHOTO_INTERVAL` + 设 `lapse_time`，
 *    一次 `startCapture()`，**相机自己按节拍**（最准、最省电、不占手机）。
 *  - **3b App 定时器**：填的值不在合法值里 → 切 `PHOTO_NORMAL`（单张拍照），
 *    由 App 侧协程每 N 秒触发一次 `startCapture()`。
 *    ——这条是为了让「间隔秒数真正可手改」，不再被固件的合法值表「定死」。
 *
 * ## SDK 2.1.5 关键事实（从 sdk-camera 的 classes.jar 反查证实）
 * connect / activeCamera / param.setValue / getValue / getSupportParam / syncAllParams /
 * startCapture / stopCapture / isWorking 全部是 **suspend 函数**，必须在协程中调用；
 * CaptureStatusListener 是 8 个方法的接口，缺一不可。
 */
class CameraController(private val context: Context) {

    interface Listener {
        fun onConnectionChanged(connected: Boolean)
        fun onCaptureStateChanged(capturing: Boolean)

        /** 拍摄文件保存完成。filePaths 相机端相对路径。 */
        fun onCaptureFinished(filePaths: List<String>)

        /**
         * **开始命令在「异步阶段」最终失败了**（模式切换被拒 / startCapture 被拒）。
         *
         * 为什么单独开一个回调：`startRecord()` 的同步前置检查能返回 Boolean，
         * 但真正的 SDK 调用是 suspend 的，等它返回时调用方早就走了。
         * 没有这个回调，一旦异步阶段失败，阈值引擎就会停在「录制中」而相机其实没动，
         * 表现为**「心率明明超标，相机却再也不响应」**（真机踩过）。
         * 编排层收到后应回滚引擎状态，让下一次心率上报重新尝试。
         */
        fun onStartFailed(reason: String)

        /**
         * 面向用户的一条消息（成功/失败都会走这里，UI 只负责显示）。
         * ⚠️ 名字叫 onError 是历史原因，实际是「消息通道」。
         */
        fun onError(msg: String)
    }

    /**
     * ⚠️ 必须是可变属性且可重建。
     *
     * 见 [release] 的长注释：`CoroutineScope.cancel()` 不可逆，
     * 一旦 cancel 后继续复用同一个 scope，本对象之后所有 `scope.launch` 都会静默失效。
     */
    private var scope: CoroutineScope = newScope()

    private fun newScope(): CoroutineScope =
        CoroutineScope(SupervisorJob() + Dispatchers.Main.immediate)

    private var device: CameraDevice? = null
    private var listener: Listener? = null

    /** 心率触发的拍摄会话是否开启。**判断「这次采集有没有开」只认这个标志。** */
    private var armed = false

    /** 相机自报是否正在拍摄。间隔类模式在两次快门之间会变 false，故不能用作会话判断。 */
    private var capturing = false

    /** App 侧定时器的任务（只在「相机不支持该间隔秒数」时启用）。 */
    private var timerJob: Job? = null

    /** App 定时器已经拍了多少张（仅用于日志）。 */
    private var shotCount = 0

    /** 当前拍摄模式（App 上可选）。 */
    var captureMode: CaptureMode = CaptureMode.VIDEO

    /** 拍照模式的间隔秒数（App 上可设，**任意正整数都支持**）。 */
    var intervalSeconds: Int = 5

    /**
     * 上次已经报过的「被挡住的原因」。
     * 心率是每秒上报的，若每次触发都重复喊「相机未连接」，日志区会被刷屏，反而看不到重点。
     */
    private var lastBlockedReason: String? = null

    /**
     * 上次「异步阶段失败」的时间戳。
     *
     * 失败后会回滚引擎，于是**下一次心率上报（1 秒后）立刻会再试一次**。
     * 若失败原因是持续性的（比如相机就不支持该模式），会变成每秒刷一条错误。
     * 这里做 3 秒冷却，把重试频率压到可接受范围，同时保留「相机一连上就自动开始」的能力。
     */
    private var lastAsyncFailAt = 0L

    /** 探测到的 lapse_time 信息（连接后探一次缓存，避免每次都问相机）。 */
    private var lapseInfo: LapseInfo? = null

    /**
     * 已连接相机的**真实机型显示名**（连接成功后从 SDK 探测）。
     *
     * ⚠️ 用途**仅限界面文案**，代码不按机型做任何分支。
     *
     * GO Ultra / GO 3S 走的是同一套 SDK 与同一套接口，控制逻辑完全一致，
     * 参数能力（如 `lapse_time` 的可选值）也一律**运行时问相机**（见 [probeLapseTime]），
     * 所以没有「按机型 if-else」这种需要，也不该有。
     *
     * 探测的目的只是「别在界面上写死机型」：这台机器上实测连的是 GO 3S，
     * 而早期文案全是「连接 GO Ultra」，与实际不符，排查和演示时都会误导。
     */
    var cameraModelName: String? = null
        private set

    private class LapseInfo(
        val param: CameraParam<Any>,
        /** 数值形式，用于判断用户填的值是否合法。 */
        val numeric: List<Double>,
        /** 原始值，setValue 时要用相机自己给的类型，避免类型不匹配被拒。 */
        val raw: List<Any>,
    )

    // ---- 连接 ----

    /** 只支持 WiFi 直连（当前工程最稳路径）。 */
    fun connectToWifiCamera(listener: Listener) {
        this.listener = listener

        // 重连前先把旧会话释放干净：相机会话是独占的，
        // 残留会话会让新的 connect 被拒或一直 pending。
        if (device != null) {
            runCatching { device?.release() }
            device = null
        }
        cancelTimer()
        armed = false
        capturing = false
        lastBlockedReason = null
        // 换相机了，之前探测的参数表作废
        lapseInfo = null

        val netId = wlan0NetworkId()
        if (netId == -1L) {
            notifyError(
                "相机连接失败：手机当前没有连上 wlan0（相机热点）。\n" +
                    "请先到系统 WiFi 里连上相机的热点（名字形如 `GO 3S xxxx.OSC`），再回来点「连接相机」。"
            )
            notifyConnection(false)
            return
        }

        val camera = CameraDevice.get(ConnectType.WIFI)
        device = camera
        attachCaptureListener(camera)
        notifyError("正在连接相机（WiFi 直连 netId=$netId）…")

        scope.launch {
            val result = camera.connect(netId)
            if (result.isSuccess) {
                // 连上后问一次机型：界面文案要如实反映当前连的是哪台
                cameraModelName = runCatching { camera.system.fetchCameraType() }
                    .getOrNull()
                    ?.getOrNull()
                    ?.displayName
                val label = cameraModelName ?: "相机"
                Timber.i("相机 WiFi 直连成功，机型=%s", cameraModelName)
                notifyConnection(true)
                notifyError("✅ 已连接 $label（WiFi 直连）")
            } else {
                val e = result.exceptionOrNull()
                Timber.w(e, "相机连接失败")
                notifyError("❌ 相机连接失败：${e?.message ?: "未知原因"}")
                notifyConnection(false)
            }
        }
    }

    /**
     * 激活相机。必须在连接后调用一次（控制 GO Ultra 的前置条件）。
     * 注意：本调用走手机公网 openapi.insta360.com，连接着相机热点时手机需另有蜂窝/其他通道可上公网。
     */
    fun activate(appId: String, secretKey: String) {
        val dev = device ?: run {
            notifyError("相机未连接，无法激活")
            return
        }
        scope.launch {
            NanProcessNetworkBinding.awaitDefaultNetwork(context)
            dev.system.activeCamera(appId, secretKey)
                .onSuccess {
                    Timber.i("activeCamera 成功")
                    notifyError("✅ 激活成功")
                }
                .onFailure {
                    Timber.w(it, "activeCamera 失败")
                    notifyError("❌ 激活失败：${it.message}")
                }
        }
    }

    // ---- 拍摄 ----

    /**
     * 开始拍摄（心率越过开拍阈值时调用）。
     *
     * @return 是否**接受了本次开始命令**（同步前置检查通过）。
     *   false 时调用方（HeartbeatOrchestrator）**必须回滚阈值引擎状态**，
     *   否则引擎会停在「录制中」而相机一直没动，之后再也不重发 START —— 真机踩过。
     *
     * ⚠️ 为什么前置检查要同步返回：调用方需要在**同一次心率上报里**决定要不要告诉 UI「已开拍」。
     * 早期版本先无条件通知 UI「触发开拍」再调本方法，结果相机没连上时界面照样显示「已开拍」，
     * 严重误导排查（用户看到的是「界面说开拍了但相机没动」）。
     */
    fun startRecord(): Boolean {
        val dev = device
        if (dev == null) {
            reportBlocked("⚠ 无法开拍：相机未连接。请先点「连接相机（WiFi 直连）」。")
            return false
        }
        if (!isConnected()) {
            reportBlocked("⚠ 无法开拍：相机连接已断开，请重新点「连接相机」。")
            return false
        }
        if (armed) {
            // 会话已经开着：算「接受」，不重复下发（重复 startCapture 会被相机拒绝）
            return true
        }
        // 上一条异步失败命令的冷却期内静默跳过，避免失败时每秒刷屏
        if (SystemClock.elapsedRealtime() - lastAsyncFailAt < ASYNC_FAIL_COOLDOWN_MS) {
            return false
        }
        lastBlockedReason = null

        val mode = captureMode
        val secs = intervalSeconds
        armed = true
        notifyError(
            if (mode.usesInterval) {
                "▶ 开始命令下发中：间隔拍照，目标每 ${secs}s 一张…"
            } else {
                "▶ 开始命令下发中：录像…"
            }
        )

        scope.launch {
            runCatching { beginSession(dev, mode, secs) }
                .onFailure {
                    Timber.w(it, "beginSession 异常")
                    failStart("❌ 开始拍摄失败：${it.message}")
                }
        }
        return true
    }

    /** 真正把「模式 → 参数 → 开始」这条链走完。全部是 suspend 调用。 */
    private suspend fun beginSession(dev: CameraDevice, mode: CaptureMode, secs: Int) {
        // 1) 切到目标模式
        var err: String? = null
        val switched = runCatching {
            dev.capture.functionMode
                .setValue(mode.functionMode)
                .onFailure { err = it.message }
                .isSuccess
        }.getOrElse {
            err = it.message
            false
        }
        if (!switched) {
            failStart("❌ 切换拍摄模式失败（目标=${mode.functionMode}）：${err ?: "未知原因"}")
            return
        }
        Timber.i("functionMode 已切到 %s", mode.functionMode)
        if (!armed) return

        // 2) ⚠️ 关键一步：切模式后必须重新同步参数列表。
        //    相机参数是「模式相关」的（例如 lapse_time 只在间隔拍照模式下才存在），
        //    官方 demo 在每次 setValue(functionMode) 之后都会调 syncAllParams()。
        //    漏掉这步 → getSupportParam() 返回的还是旧模式的参数表 → 找不到 lapse_time
        //    → 间隔永远设不上，表现为「选了间隔拍照但按录像的节奏走 / 报参数不可用」。
        runCatching { dev.capture.syncAllParams() }
            .onSuccess { Timber.i("syncAllParams 完成") }
            .onFailure {
                Timber.w(it, "syncAllParams 失败")
                notifyError("⚠ 同步相机参数失败：${it.message}")
            }
        if (!armed) return

        if (!mode.usesInterval) {
            fireStart(dev, mode)
            return
        }

        // 3) 间隔拍照：优先用固件原生间隔；相机不支持用户填的秒数时退回 App 定时器
        val info = probeLapseTime(dev)
        val wantsDouble = secs.toDouble()
        if (info != null && info.numeric.contains(wantsDouble)) {
            setFirmwareInterval(info, secs)
            if (!armed) return
            fireStart(dev, mode)
        } else {
            notifyError(
                "ℹ 相机不支持「每 ${secs}s」这个间隔（支持：${info?.numeric?.joinToString(" / ") { fmt(it) } ?: "未知"}），" +
                    "改用 App 定时器实现 —— 每 ${secs}s 触发一次单张拍照。"
            )
            startAppTimerCapture(dev, secs)
        }
    }

    /** 固件原生路径：下发一次开始命令，相机自己按节拍。 */
    private suspend fun fireStart(dev: CameraDevice, mode: CaptureMode) {
        runCatching { dev.capture.startCapture() }
            .onSuccess {
                Timber.i("startCapture 下发成功 mode=%s interval=%ds", mode, intervalSeconds)
                notifyError("✅ 已下发开始命令（${mode.label}）")
            }
            .onFailure {
                failStart("❌ 开始拍摄失败：${it.message}")
            }
    }

    /**
     * App 定时器路径：切到单张拍照，然后**由 App 每 N 秒触发一次** `startCapture()`。
     *
     * 只在「相机固件不支持用户填的间隔秒数」时启用，目的是让间隔秒数**真正可任意手改**。
     * 代价是每次快门要一次 WiFi 往返（比固件原生略费电、精度略差），但换来的是完全自由的秒数。
     */
    private suspend fun startAppTimerCapture(dev: CameraDevice, secs: Int) {
        val switched = runCatching {
            // 单张拍照模式（不是 PHOTO_INTERVAL）：一次 startCapture 拍一张
            dev.capture.functionMode.setValue(FunctionMode.PHOTO_NORMAL).isSuccess
        }.getOrDefault(false)

        if (!switched) {
            notifyError("⚠ 无法切到单张拍照模式，定时拍照可能不可用。")
        } else {
            Timber.i("functionMode 已切到 PHOTO_NORMAL（App 定时器路径）")
        }

        cancelTimer()
        armed = true
        shotCount = 0

        timerJob = scope.launch {
            // 切完模式同样要同步参数表（保持一致，避免后续取参取到旧表）
            runCatching { dev.capture.syncAllParams() }
            notifyError("▶ App 定时拍照已启动：每 ${secs}s 一张，心率回落后自动停止。")

            var busyTicks = 0
            while (isActive && armed) {
                if (capturing) {
                    // 上一张还在拍/存盘：等它，别把命令堆在一起（相机侧会互相打架）
                    busyTicks++
                    if (busyTicks <= BUSY_MAX_TICKS) {
                        delay(BUSY_POLL_MS)
                        continue
                    }
                    // 长时间为「忙」→ 多半是状态回调漏了，别卡死，继续下一张
                    Timber.w("app timer: 相机连续 %d ms 报忙，强制继续", BUSY_MAX_TICKS * BUSY_POLL_MS)
                    busyTicks = 0
                } else {
                    busyTicks = 0
                }

                if (!armed) break
                runCatching { dev.capture.startCapture() }
                    .onSuccess {
                        shotCount++
                        Timber.i("app timer: 第 %d 张已下发", shotCount)
                        notifyError("📷 已触发第 ${shotCount} 张（每 ${secs}s 一张）")
                    }
                    .onFailure {
                        Timber.w(it, "app timer startCapture 失败")
                        notifyError("❌ 定时拍照失败：${it.message}")
                    }
                delay(secs * 1000L)
            }
        }
    }

    /**
     * 把「间隔秒数」写到相机的 `lapse_time`。仅在用户填的值确实在合法值里时被调用。
     *
     * 用 `getSupported()` 拿相机**自己报上来**的合法值列表，而不是硬编码，
     * 这样换机型（X4/X5 等）也能自适应。
     */
    private suspend fun setFirmwareInterval(info: LapseInfo, seconds: Int) {
        val idx = info.numeric.indexOfFirst { it == seconds.toDouble() }
        if (idx < 0) return
        runCatching { info.param.setValue(info.raw[idx]) }
            .onSuccess { notifyError("✅ 间隔已设为 $seconds s（相机固件执行）") }
            .onFailure {
                Timber.w(it, "setValue(lapse_time) 失败")
                notifyError("❌ 设置间隔失败：${it.message}")
            }
    }

    /** 探测 `lapse_time` 的合法值（探一次缓存）。探测不到返回 null。 */
    private suspend fun probeLapseTime(dev: CameraDevice): LapseInfo? {
        lapseInfo?.let { return it }

        val params = runCatching { dev.capture.getSupportParam() }.getOrElse {
            notifyError("⚠ 读取相机参数失败：${it.message}")
            return null
        }
        val param = params.firstOrNull { it.getName() == "lapse_time" }
        if (param == null) {
            // 把实际可用的参数名全列出来 —— 机型差异时这行日志能直接告诉我们真实 key
            notifyError(
                "⚠ 相机未提供 lapse_time 参数。当前可用参数：${params.joinToString { it.getName() }}"
            )
            return null
        }

        @Suppress("UNCHECKED_CAST")
        val anyParam = param as CameraParam<Any>
        val raw = runCatching { anyParam.getSupported().getOrNull() }.getOrNull().orEmpty()
        val numeric = raw.mapNotNull { it.toString().toDoubleOrNull() }
        if (numeric.isEmpty()) {
            notifyError("⚠ 无法获取间隔可选值，改用 App 定时器。")
            return null
        }
        Timber.i("lapse_time 合法值 = %s", numeric)
        return LapseInfo(anyParam, numeric, raw).also { lapseInfo = it }
    }

    /** 把 5.0 显示成 "5"，0.5 显示成 "0.5"。 */
    private fun fmt(v: Double): String =
        if (v == v.toLong().toDouble()) v.toLong().toString() else v.toString()

    /**
     * 停止拍摄（录像停止 / 间隔拍照停止，都走同一个命令）。
     *
     * ⚠️ 判断依据是 [armed]（心率触发会话），**不是** [capturing]。
     * 间隔拍照在两次快门之间 `capturing` 会是 false，若据此判定「没在拍」就会漏发停止命令，
     * 结果是「界面显示已暂停、相机还在一直拍」。
     */
    fun stopRecord() {
        val dev = device
        // App 定时器必须无条件取消，否则定时器会在「已停止」后继续触发快门
        cancelTimer()

        if (dev == null) {
            if (armed) {
                armed = false
                notifyError("停止命令忽略：相机未连接")
            }
            return
        }

        if (!armed) {
            // 会话没开，但相机可能仍在拍（漏收状态回调）→ 补一次停止
            scope.launch {
                val really = runCatching { dev.capture.isWorking() }.getOrDefault(false)
                if (!really) return@launch
                notifyError("ℹ 检测到相机实际仍在拍摄（状态回调遗漏），已补发停止命令")
                runCatching { dev.capture.stopCapture() }
                    .onFailure { notifyError("❌ 补发停止失败：${it.message}") }
            }
            return
        }

        armed = false
        notifyError(
            if (captureMode.usesInterval) "⏹ 停止命令下发中：停止间隔拍照…" else "⏹ 停止命令下发中：停止录像…"
        )

        scope.launch {
            runCatching { dev.capture.stopCapture() }
                .onSuccess { Timber.i("stopCapture 下发成功") }
                .onFailure {
                    Timber.w(it, "stopCapture 失败")
                    notifyError("❌ 停止拍摄失败：${it.message}")
                }
        }
    }

    fun isConnected(): Boolean =
        runCatching { device?.isConnected() == true }.getOrDefault(false)

    /**
     * 是否处于「心率触发的拍摄会话」中。
     *
     * ⚠️ 注意与「相机自报在拍」的区别：间隔拍照/延时类模式的两次快门之间，
     * 相机自报的状态是「没在拍」，但会话其实还开着。判定「会话开没开」只能看这个标志。
     *
     * 供 Activity 重建后回填 UI（编排者是进程级单例，会话可能仍在进行）。
     */
    fun isArmed(): Boolean = armed

    // ---- 相机素材（.lrv / .mp4 列出与下载） ----
    //
    // 用的是 **sdk-camera 自带**的 `CameraDevice.file`（= `CameraFile`），
    // 不是 sdk-media。理由：sdk-media 的传递依赖 bmgmedia 会带进约 155MB 原生库，
    // 只为了「列文件 + 下一段视频」不值得把 APK 从 35MB 撑到 235MB。
    //
    // 依据（javap 反查 classes.jar 实证，2.1.5）：
    //   · CameraFile.listMediaFiles(MediaFileType, includeRecording) : suspend -> Result<List<String>>
    //   · CameraFile.listMediaFiles(MediaFileType, offset, count, includeRecording) : suspend -> Result<Pair<List<String>, Int>>
    //   · CameraFile.getFileInfoList() : suspend -> Result<List<FileInfo>>   （FileInfo.url）
    //   · CameraFile.downloadMediaFile(remotePath, destPath, onProgress) : suspend -> Result<String>
    //   · UnifiedTransport 的 SUPPORT_CAMERA_FILE_EXTENSION 含 lrv/mp4/insv/jpg 等 → LRV 是被支持的素材类型
    //
    // ⚠️ 官方 demo 只用了 sdk-media 的高层封装（WorkManager.getAllCameraWorks），
    //    没有直接调这几个低层接口，所以**返回格式要靠日志实机确认**。
    //    因此这里做了多重兜底：多个 MediaFileType 取并集 + 空则翻页重试，全部结果原样打日志。

    /**
     * 相机上的一个素材条目。
     *
     * ⚠️ **必须有 [url]**：SDK 的 `downloadMediaFile` 第一个参数名叫 `url`，
     * 内部会先 `getFilenameFromUrl()` 解析它（拿文件名拼到目标目录下）。
     * 传 `/DCIM/Camera01/xxx.lrv` 这种裸路径时解析失败，SDK 会抛出
     * `DownloadCameraFileException("Invalid URL")` —— 这正是 v12 上传失败的根因。
     */
    data class MediaItem(
        /** 完整下载地址（`FileInfo.url`，或由 host + 路径兜底拼出）。 */
        val url: String,
        /** 相机侧路径（形如 `/DCIM/Camera01/LRV_xxx.lrv`）。 */
        val path: String,
        /** 文件名。 */
        val name: String,
    ) {
        /** 小写扩展名，不含点。 */
        val ext: String get() = name.substringAfterLast('.', "").lowercase()

        val isLrv: Boolean get() = ext == "lrv"
        val isVideo: Boolean get() = ext in VIDEO_EXTS
        val isPhoto: Boolean get() = ext in PHOTO_EXTS

        /** 名字里的 `yyyyMMdd_HHmmss`，拿来做时间排序；抓不到就退化为文件名。 */
        val sortKey: String get() = TS_REGEX.find(name)?.value ?: name

        /** 界面上的类型标签。 */
        val kindLabel: String
            get() = when {
                isLrv -> "视频·LRV"
                ext == "mp4" || ext == "mov" -> "视频·MP4"
                ext == "insv" || ext == "insp" -> "视频·全景"
                ext == "jpg" || ext == "jpeg" -> "照片·JPG"
                ext == "dng" || ext == "raw" -> "照片·RAW"
                ext.isBlank() -> "未知"
                else -> ext.uppercase()
            }
    }

    /**
     * 列出相机上**所有可下载的素材**（视频在前、各自按时间倒序）。
     *
     * ## 为什么要两条路一起问
     *
     * - **路线 A（主）**：`getFileInfoList()` 返回 `FileInfo`，其中 `url` 是**唯一能被
     *   `downloadMediaFile` 接受的入参**。所以素材列表必须以它为准。
     * - **路线 B（补）**：`listMediaFiles()` 只给路径不给 URL，但个别固件可能只在它这边
     *   给全量（反之亦然）。两条路按**文件名**去重合并，最稳。
     *
     * 任一接口抛异常都不影响另一条路。
     */
    suspend fun listCameraMediaItems(): Result<List<MediaItem>> {
        val dev = device ?: return Result.failure(IllegalStateException("相机未连接"))
        val file = dev.file
        val items = LinkedHashMap<String, MediaItem>()
        val notes = mutableListOf<String>()

        // ── 路线 A：FileInfo.url（可下载）
        runCatching { file.getFileInfoList() }
            .onSuccess { r ->
                r.onSuccess { infos ->
                    notes += "fileInfo=${infos.size}"
                    for (fi in infos) {
                        // getUrl() 在 Kotlin 里是平台类型（非空），但固件万一给空串就得跳过，
                        // 所以用 runCatching + takeIf 兜一层，而不是写 `?: continue`（那会有编译警告）
                        val raw = runCatching { fi.url }.getOrNull()?.takeIf { it.isNotBlank() } ?: continue
                        val name = raw.substringAfterLast('/').substringBefore('?')
                        if (name.isBlank()) continue
                        items.putIfAbsent(
                            name,
                            MediaItem(url = normalizeMediaUrl(raw), path = pathOf(raw), name = name),
                        )
                    }
                }.onFailure { notes += "fileInfo=错误(${it.message})" }
            }
            .onFailure { notes += "fileInfo=异常(${it.message})" }

        // ── 路线 B：listMediaFiles 的路径（自拼 URL 兜底）
        val hostHint = cameraHostFromSdk()
        val types = listOf(
            MediaFileType.VIDEO,
            MediaFileType.VIDEO_AND_PHOTO,
            MediaFileType.MP4,
            MediaFileType.PHOTO,
        )
        for (t in types) {
            runCatching { file.listMediaFiles(t, false) }
                .onSuccess { r ->
                    r.onSuccess { list ->
                        notes += "$t=${list.size}"
                        for (p in list) {
                            val name = p.substringAfterLast('/')
                            if (name.isBlank()) continue
                            items.putIfAbsent(
                                name,
                                MediaItem(url = normalizeMediaUrl(p, hostHint), path = p, name = name),
                            )
                        }
                    }.onFailure { notes += "$t=错误(${it.message})" }
                }
                .onFailure { notes += "$t=异常(${it.message})" }
        }

        // 视频优先、其次按时间倒序 —— 用户最常用「刚录的那段」，放最前面
        val sorted = items.values.sortedWith(
            compareByDescending<MediaItem> { it.isVideo }.thenByDescending { it.sortKey }
        )
        val videoCount = sorted.count { it.isVideo }
        val lrvCount = sorted.count { it.isLrv }
        Timber.i(
            "素材枚举：%s；去重后 %d 条（视频 %d / LRV %d）",
            notes.joinToString(" "), sorted.size, videoCount, lrvCount,
        )
        // 前几条完整打印，供真机核对 url 格式
        sorted.take(5).forEach { Timber.i("  样例：%s → %s", it.name, it.url) }
        notifyError(
            "📂 相机素材：${notes.joinToString("｜")}（共 ${sorted.size} 条：视频 $videoCount、LRV $lrvCount）"
        )
        if (sorted.isEmpty()) {
            return Result.failure(IllegalStateException("相机返回 0 个素材（明细见上一行日志）"))
        }
        return Result.success(sorted)
    }

    /**
     * 把「可能是路径、也可能是 URL」的素材地址统一成完整 URL。
     *
     * 实测路径：`FileInfo.url` 给的是完整地址；但 `listMediaFiles` 只给 `/DCIM/...`，
     * 这时用 [hostHint]（= `file.host`，如 `http://192.168.42.1`）拼。
     */
    private fun normalizeMediaUrl(raw: String, hostHint: String? = null): String {
        if (raw.startsWith("http://", true) || raw.startsWith("https://", true)) return raw
        val host = (hostHint ?: cameraHostFromSdk())
            ?.trimEnd('/')
            ?.takeIf { it.isNotBlank() }
            ?: DEFAULT_CAMERA_HOST
        return host + if (raw.startsWith("/")) raw else "/$raw"
    }

    /**
     * 从 SDK 读相机文件服务的 host（形如 `http://192.168.42.1`）。
     *
     * ⚠️ **必须反射**：这几个 getter 在 Kotlin 元数据里不是属性，直接写 `file.host`
     * 编译不过（实测报 `Unresolved reference 'host'`）。反射拿不到就返回 null，由调用方兜底。
     */
    private fun cameraHostFromSdk(): String? = runCatching {
        val f = device?.file ?: return@runCatching null
        f.javaClass.getMethod("getHost").invoke(f) as? String
    }.getOrNull()

    /** 从任意形式的素材地址里取出相机侧路径。 */
    private fun pathOf(raw: String): String =
        if (raw.startsWith("http://", true) || raw.startsWith("https://", true)) {
            val noScheme = raw.substringAfter("://")
            val slash = noScheme.indexOf('/')
            if (slash >= 0) noScheme.substring(slash) else "/"
        } else {
            raw
        }

    /**
     * 从相机下载一个素材到手机本地。
     *
     * ⚠️ **参数语义是反直觉的，两个都别搞错**（javap 反汇编 `CameraFileService` 实证）：
     *  - 第 1 个参数是 **URL**（`downloadMediaFile(String url, ...)`），SDK 内部先
     *    `getFilenameFromUrl(url)` 取名字，再拼到第 2 个参数下面；
     *  - 第 2 个参数是 **目标「目录」**，不是目标文件的完整路径。
     *
     * 早期版本传的是「相机侧裸路径 + 目标文件全路径」，于是 SDK 在第一步解析 URL 时就失败，
     * 抛出 `DownloadCameraFileException("Invalid URL")` —— 界面上的表现就是
     * 「⬇ 开始下载 → ❌ 下载失败：Invalid URL」，且**一个字节都没传**。
     *
     * @param url       素材地址（[MediaItem.url]，裸路径也能自动补全）
     * @param targetDir 落地目录（不存在会自动创建）
     * @return 实际落盘的文件路径
     */
    suspend fun downloadCameraMedia(
        url: String,
        targetDir: File,
        onProgress: (Long, Long) -> Unit,
    ): Result<String> {
        val dev = device ?: return Result.failure(IllegalStateException("相机未连接"))
        targetDir.mkdirs()
        val before = targetDir.listFiles()?.map { it.name }?.toHashSet() ?: hashSetOf()

        val fullUrl = normalizeMediaUrl(url)
        Timber.i("开始下载：url=%s → dir=%s", fullUrl, targetDir.absolutePath)

        // ⚠️ downloadMediaFile 本身返回 Result<String>（失败不抛异常），
        //    外面再套一层 runCatching 会得到 Result<Result<String>>，类型就错了。
        val call: Result<String> =
            dev.file.downloadMediaFile(fullUrl, targetDir.absolutePath, onProgress)
        val raw: String = call.getOrElse { e -> return Result.failure(e) }

        // SDK 返回的是落盘路径。正常情况下直接信它；万一它返回了个不存在的路径
        // （不同固件行为可能有差异），就退回「目录里新增的、最新的那个文件」。
        val direct = runCatching { File(raw) }.getOrNull()
        if (direct != null && direct.isFile && direct.length() > 0) {
            return Result.success(direct.absolutePath)
        }
        val fallback = targetDir.listFiles()
            ?.filter { it.isFile && it.length() > 0 && it.name !in before }
            ?.maxByOrNull { it.lastModified() }
        Timber.w("downloadMediaFile 返回 %s（不是有效文件），改用目录扫描结果：%s", raw, fallback?.name)
        return if (fallback != null) {
            Result.success(fallback.absolutePath)
        } else {
            Result.failure(IllegalStateException("下载返回「$raw」，但目录里没找到有效文件"))
        }
    }

    /**
     * 相机文件服务的接入点，仅用于排查（固件差异时这行日志很关键）。
     *
     * ⚠️ 这里**故意用反射**读 endpoint/host/prot：SDK 里这几个 getter 的拼写并不规范
     * （javap 实测是 `getEndpoint()` / `getHost()` / **`getProt()`**，最后一个是 `port` 的笔误），
     * 直接写属性名有编译不过的风险，反射拿不到就是 null，不影响主流程。
     */
    fun cameraFileEndpointDebug(): String {
        val f = runCatching { device?.file }.getOrNull() ?: return "未连接"
        fun read(name: String): String? =
            runCatching { f.javaClass.getMethod(name).invoke(f)?.toString() }.getOrNull()
        return "endpoint=${read("getEndpoint")} host=${read("getHost")} port=${read("getProt")}"
    }

    fun disconnect() {
        cancelTimer()
        runCatching { device?.release() }
        device = null
        armed = false
        capturing = false
        lastBlockedReason = null
        lapseInfo = null
    }

    /**
     * 释放协程作用域（Activity 销毁 / 用户点「停止/断开」时调用）。
     *
     * ⚠️⚠️ **这里必须重建 scope，不能只 cancel。**
     *
     * `CoroutineScope.cancel()` 是不可逆的。早期版本 `release()` 里 cancel 完
     * 仍然继续复用同一个 scope，导致一个极难查的后果：
     * **只要用户点过一次「停止/断开」，本对象之后所有的 `scope.launch {}` 全部静默不执行。**
     *
     * 具体表现（就是真机上遇到的「心率到阈值了但相机完全不动」）：
     *   · 点「连接 GO Ultra」毫无反应 —— connect 的协程根本不会跑
     *   · 心率冲过阈值，`startRecord()` 里的模式切换 / startCapture 一行都不执行
     *   · **而且一句报错都没有** —— 因为代码根本没有执行到会报错的地方
     *
     * 因为 cancel 只对「当前这个 scope 实例」生效，cancel 后换一个新实例即可复用本对象。
     */
    fun release() {
        disconnect()
        runCatching { scope.cancel() }
        scope = newScope()
    }

    // ---- 内部工具 ----

    private fun cancelTimer() {
        timerJob?.cancel()
        timerJob = null
    }

    /**
     * 异步阶段失败：回滚会话标志 + 通知编排层回滚阈值引擎。
     *
     * 这是「心率超标但相机再也不响应」的根治点 ——
     * 没有它，引擎会永久停在 REC，再也不会重发 START。
     */
    private fun failStart(reason: String) {
        armed = false
        cancelTimer()
        lastBlockedReason = null
        lastAsyncFailAt = SystemClock.elapsedRealtime()
        notifyError(reason)
        listener?.onStartFailed(reason)
    }

    /** 同一原因只报一次，避免心率每秒上报时把日志刷爆。 */
    private fun reportBlocked(reason: String) {
        if (lastBlockedReason == reason) return
        lastBlockedReason = reason
        notifyError(reason)
    }

    private fun attachCaptureListener(camera: CameraDevice) {
        camera.capture.registerCaptureStatusListener(
            object : CaptureStatusListener {
                override fun onCaptureStarting(functionMode: FunctionMode) {
                    Timber.i("onCaptureStarting mode=%s", functionMode)
                    capturing = true
                    lastBlockedReason = null
                    notifyCaptureState(true)
                }

                override fun onCaptureWorking(functionMode: FunctionMode) {
                    capturing = true
                    lastBlockedReason = null
                    notifyCaptureState(true)
                }

                override fun onCaptureStopping(functionMode: FunctionMode) {
                    // 停止中，等 onCaptureFinish 再翻状态
                    Timber.i("onCaptureStopping mode=%s", functionMode)
                }

                override fun onCaptureFinish(
                    functionMode: FunctionMode,
                    filePaths: List<String>,
                ) {
                    Timber.i("onCaptureFinish mode=%s files=%d", functionMode, filePaths.size)
                    capturing = false
                    // ⚠️ 只有「不在 App 定时器路径」时才把界面翻成暂停：
                    // 定时拍照每张之间都会 onCaptureFinish 一次，翻界面会闪成「已暂停」。
                    if (timerJob == null) notifyCaptureState(false)
                    notifyFinished(filePaths)
                }

                override fun onCaptureError(
                    functionMode: FunctionMode,
                    throwable: Throwable,
                ) {
                    Timber.w(throwable, "onCaptureError mode=%s", functionMode)
                    capturing = false
                    notifyCaptureState(false)
                    notifyError("❌ 拍摄出错：${throwable.message}")
                }

                override fun onCaptureTimeChanged(
                    functionMode: FunctionMode,
                    captureTime: Long,
                ) {
                    // 录制计时，当前 UI 不展示
                }

                override fun onCaptureCountChanged(
                    functionMode: FunctionMode,
                    captureCount: Int,
                ) {
                    // 张数变化（拍照/延时类模式），当前 UI 不展示
                }

                override fun onCaptureSubStatusChanged(
                    functionMode: FunctionMode,
                    subStatus: CameraCaptureStatus.SubStatus,
                ) {
                    // 子状态（曝光/存盘/取消等），当前 UI 不展示
                }
            }
        )
    }

    // ---- 回调分发 ----

    private fun notifyConnection(connected: Boolean) {
        listener?.onConnectionChanged(connected)
    }

    private fun notifyCaptureState(capturing: Boolean) {
        listener?.onCaptureStateChanged(capturing)
    }

    private fun notifyFinished(files: List<String>) {
        listener?.onCaptureFinished(files)
    }

    private fun notifyError(msg: String) {
        Timber.i("camera: %s", msg)
        listener?.onError(msg)
    }

    /**
     * 找到当前已连接 WiFi 的 networkId（wlan0）。
     *
     * `allNetworks` 在 API 31+ 标了 deprecated，但**没有等价的同步替代品**
     * （官方建议改用 registerNetworkCallback 异步跟踪，那会把这里变成有状态的回调管理）。
     * 这里只是「连相机热点前问一次当前 wlan0」，同步语义正是需要的，故保留并显式抑制警告。
     */
    @Suppress("DEPRECATION")
    private fun wlan0NetworkId(): Long {
        val cm = context.getSystemService(Context.CONNECTIVITY_SERVICE) as ConnectivityManager
        return cm.allNetworks.firstOrNull { net ->
            val caps = cm.getNetworkCapabilities(net) ?: return@firstOrNull false
            if (!caps.hasTransport(NetworkCapabilities.TRANSPORT_WIFI)) return@firstOrNull false
            val lp: LinkProperties = cm.getLinkProperties(net) ?: return@firstOrNull false
            lp.interfaceName == "wlan0"
        }?.networkHandle ?: -1L
    }

    private companion object {
        /** 异步失败后的冷却：压住重试频率，同时保留「相机一连上就自动开始」。 */
        const val ASYNC_FAIL_COOLDOWN_MS = 3_000L

        /** App 定时器：相机报「忙」时的轮询间隔。 */
        const val BUSY_POLL_MS = 200L

        /** 连续忙多久后放弃等待（200ms × 50 = 10s），防止状态回调丢失时永久卡住。 */
        const val BUSY_MAX_TICKS = 50

        /**
         * `file.host` 拿不到时的兜底相机地址。
         * GO 系列的 AP 网关固定是 `192.168.42.1`（真机实测），所以这个兜底是可靠的。
         */
        const val DEFAULT_CAMERA_HOST = "http://192.168.42.1"

        /** 视为「视频」的扩展名。 */
        val VIDEO_EXTS = setOf("mp4", "mov", "lrv", "insv", "insp")

        /** 视为「照片」的扩展名。 */
        val PHOTO_EXTS = setOf("jpg", "jpeg", "dng", "raw")

        /** 文件名里的时间戳，形如 `20260923_160523`。 */
        val TS_REGEX = Regex("""\d{8}_\d{6}""")
    }
}
