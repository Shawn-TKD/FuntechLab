package com.insta360.heartbeat.settings

import android.content.Context
import com.insta360.heartbeat.camera.CaptureMode

/**
 * 用户在 App 上手动设置的参数（阈值 / 间隔 / 模式），存 SharedPreferences 持久化。
 *
 * 为什么单独抽一个类：
 *  - 之前阈值是 `ThresholdEngine(onThreshold = 110, offThreshold = 100)` 写死在代码里，改一次要重新打包。
 *  - 现在界面上可改，必须持久化，否则每次冷启动都回到默认值，演示时很尴尬。
 *
 * 取值合法性在 [save] 里做统一钳制（clamp），避免 UI 输入越界传到相机侧。
 */
class AppSettings(context: Context) {

    private val prefs =
        context.applicationContext.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)

    /** 开录阈值（心率 ≥ 此值开录）。 */
    var onThreshold: Int
        get() = prefs.getInt(KEY_ON, DEFAULT_ON)
        set(v) = prefs.edit().putInt(KEY_ON, v.coerceIn(MIN_HR, MAX_HR)).apply()

    /** 暂停阈值（心率 < 此值暂停）。 */
    var offThreshold: Int
        get() = prefs.getInt(KEY_OFF, DEFAULT_OFF)
        set(v) = prefs.edit().putInt(KEY_OFF, v.coerceIn(MIN_HR, MAX_HR)).apply()

    /** 拍照模式下的间隔秒数。 */
    var intervalSeconds: Int
        get() = prefs.getInt(KEY_INTERVAL, DEFAULT_INTERVAL)
        set(v) = prefs.edit().putInt(KEY_INTERVAL, v.coerceIn(MIN_INTERVAL, MAX_INTERVAL)).apply()

    /** 当前选中的拍摄模式。 */
    var captureMode: CaptureMode
        get() = CaptureMode.fromName(prefs.getString(KEY_MODE, null))
        set(v) = prefs.edit().putString(KEY_MODE, v.name).apply()

    /**
     * 素材上传的服务器地址。
     *
     * 默认值 = **白日梦想家（daydreamer）服务的素材投递端点** `https://your-server.example.com/api/jobs`：
     * 请求体是**视频原始字节**，靠 `Authorization: Bearer <token>` 鉴权，
     * 上传成功后服务端建一个转码/生成任务，**用 GET 同一个地址**即可查任务状态。
     *
     * ⚠️ 与早期版本协议不同：早期是 `http://host:8000/upload` 的 multipart 表单
     * （那是我们自己的调试服务 `server/upload_server.py`）。要回到本地调试就把地址改回
     * `http://127.0.0.1:8000/upload` 并执行 `adb reverse tcp:8000 tcp:8000`（见 docs/素材上传.md）。
     */
    var uploadUrl: String
        get() = prefs.getString(KEY_UPLOAD_URL, DEFAULT_UPLOAD_URL).orEmpty()
        set(v) = prefs.edit().putString(KEY_UPLOAD_URL, v.trim()).apply()

    /**
     * 上传鉴权令牌，随请求头 `Authorization: Bearer <token>` 发送。
     *
     * daydreamer 服务端用 `hmac.compare_digest` 比对，且**独立于浏览器的「连接码」会话** ——
     * 手机端不需要先访问 /connect 换 cookie，带上这个令牌就能直接投递。
     */
    var uploadToken: String
        get() = prefs.getString(KEY_UPLOAD_TOKEN, DEFAULT_UPLOAD_TOKEN).orEmpty()
        set(v) = prefs.edit().putString(KEY_UPLOAD_TOKEN, v.trim()).apply()

    /**
     * 本机上传过的任务 id（新的在前）。
     *
     * ## 为什么客户端必须自己记
     *
     * daydreamer 的上传令牌是**受限凭据**。服务端 `server.py` 的 `route()` 里，
     * 走 `upload_access` 分支时只放行两件事：
     * ```
     * if path == "/api/jobs" and self.command == "POST":  return self.upload()   # 提交
     * if re.fullmatch("/api/jobs/" + ID, path) and GET:   ...                    # 查指定任务
     * raise APIError(403, "上传凭据仅可上传视频及查询已知任务。")                    # 其它一律拒
     * ```
     * 也就是说 **`GET /api/jobs`（列全部）会拿到 403** —— 想列全部得用浏览器
     * 「连接码」换 session cookie，那是更大的权限，不该塞进手机端。
     *
     * 所以：上传成功时把返回的 job id 存下来，之后靠 `GET /api/jobs/{id}` 查进度。
     * 用逗号分隔即可 —— job id 是 32 位小写 hex，不含逗号。
     */
    var uploadedJobIds: List<String>
        get() = prefs.getString(KEY_JOB_IDS, "").orEmpty()
            .split(',')
            .map { it.trim() }
            .filter { it.isNotEmpty() }
        set(v) = prefs.edit()
            .putString(KEY_JOB_IDS, v.distinct().take(MAX_REMEMBERED_JOBS).joinToString(","))
            .apply()

    /** 记一条新上传的任务 id（去重、置顶、超量截断）。 */
    fun rememberJobId(id: String) {
        if (id.isBlank()) return
        uploadedJobIds = listOf(id) + uploadedJobIds.filter { it != id }
    }

    /**
     * 是否使用浅色主题。默认 false = **深色**。
     *
     * 为什么默认深色：相机类 App 在户外强光下取景，深色界面减少屏幕自身反射对取景的干扰。
     * 这个偏好会被 [com.insta360.heartbeat.HeartbeatApp] 在冷启动时读取并应用，
     * 所以开关切换后必须马上落盘。
     */
    var lightTheme: Boolean
        get() = prefs.getBoolean(KEY_LIGHT_THEME, false)
        set(v) = prefs.edit().putBoolean(KEY_LIGHT_THEME, v).apply()

    /**
     * 一次性保存，并返回钳制后的实际值（UI 用它回显，让用户看到真实生效值）。
     *
     * 特别处理：暂停阈值必须 < 开录阈值，否则迟滞失去意义
     * （会退化成「一直在阈值附近疯狂开关」）。这里若用户填反了，自动把暂停阈值压到开录阈值 - 1。
     */
    fun save(
        on: Int,
        off: Int,
        interval: Int,
        mode: CaptureMode,
    ): Saved {
        val clampedOn = on.coerceIn(MIN_HR, MAX_HR)
        var clampedOff = off.coerceIn(MIN_HR, MAX_HR)
        if (clampedOff >= clampedOn) clampedOff = clampedOn - 1
        val clampedInterval = interval.coerceIn(MIN_INTERVAL, MAX_INTERVAL)

        prefs.edit()
            .putInt(KEY_ON, clampedOn)
            .putInt(KEY_OFF, clampedOff)
            .putInt(KEY_INTERVAL, clampedInterval)
            .putString(KEY_MODE, mode.name)
            .apply()

        return Saved(clampedOn, clampedOff, clampedInterval, mode)
    }

    data class Saved(
        val onThreshold: Int,
        val offThreshold: Int,
        val intervalSeconds: Int,
        val captureMode: CaptureMode,
    )

    companion object {
        private const val PREFS_NAME = "heartbeat_settings"

        private const val KEY_ON = "on_threshold"
        private const val KEY_OFF = "off_threshold"
        private const val KEY_INTERVAL = "interval_seconds"
        private const val KEY_MODE = "capture_mode"
        private const val KEY_UPLOAD_URL = "upload_url"
        private const val KEY_UPLOAD_TOKEN = "upload_token"
        private const val KEY_JOB_IDS = "uploaded_job_ids"
        private const val KEY_LIGHT_THEME = "light_theme"

        /** 最多记住多少条上传任务（再多也没人翻，且逐个查询会变慢）。 */
        private const val MAX_REMEMBERED_JOBS = 40

        /**
         * 默认上传地址：白日梦想家（daydreamer）的素材投递端点。
         * 本地调试改回 `http://127.0.0.1:8000/upload` + `adb reverse tcp:8000 tcp:8000`。
         */
        const val DEFAULT_UPLOAD_URL = "https://your-server.example.com/api/jobs"

        /**
         * 默认上传令牌（daydreamer 服务端 `DAYDREAMER_UPLOAD_TOKEN` 对应的值）。
         * 换服务器 / 换令牌时在界面上改，不要改这里。
         */
        const val DEFAULT_UPLOAD_TOKEN = "WB3P-0KlSPcQclH9R1I-XSpJCfQ8YVjdOgiUCOb9GGs"

        /** 默认开录阈值。 */
        const val DEFAULT_ON = 110

        /** 默认暂停阈值。 */
        const val DEFAULT_OFF = 100

        /** 默认间隔秒数（对应 GO Ultra lapse_time 的合法值之一）。 */
        const val DEFAULT_INTERVAL = 5

        /**
         * 心率可设范围。
         *
         * ⚠️ 这两个值只是「防手滑」的兜底（防止填 0 或 9999 把日志刷爆），
         * 不是产品意义上的限制 —— 需求是「阈值可以在 App 上自由手改」，
         * 所以范围放到比生理极限还宽：30~250 bpm 基本覆盖任何真实/演示场景。
         * 早先写死 110/100 是代码里的常量，改一次要重新打包，那个才是真「定死」。
         */
        const val MIN_HR = 30
        const val MAX_HR = 250

        /**
         * 间隔秒数可设范围。
         *
         * ⚠️ 这里**不按相机固件的合法值表来限制输入**。
         * GO Ultra 的 lapse_time 只接受 [0,1,3,5,10,30,60,120]，
         * 但用户填别的秒数（比如 7s）也能用 —— CameraController 会自动改走
         * 「App 侧定时器 + 单张拍照」实现，所以秒数是真正可任意填的。
         */
        const val MIN_INTERVAL = 1
        const val MAX_INTERVAL = 600
    }
}
