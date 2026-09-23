package com.insta360.heartbeat.upload

import android.content.Context
import com.insta360.heartbeat.camera.CameraController
import com.insta360.heartbeat.camera.CameraController.MediaItem
import com.insta360.heartbeat.settings.AppSettings
import java.io.File
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import timber.log.Timber

/**
 * 「把相机里的素材传到服务器」这条链路的编排者。
 *
 * ## 三条入口
 *
 * | 入口 | 用途 |
 * |---|---|
 * | [refreshMediaList] | 只列素材，给界面做「选哪一条」的列表 |
 * | [uploadItem] | 上传用户**点选**的那一条 |
 * | [start] | 自动挑最新一段视频上传（快捷路径，不用先选） |
 *
 * 前两者是 v13 新增的：v12 只能自动挑「最新一条 LRV」，用户没法选择，
 * 而且因为下载参数传错（详见 [CameraController.downloadCameraMedia]）整条链路一步都没走通。
 *
 * ## 完整链路（每一步都有日志，因为真机排查只能靠屏幕日志）
 *
 * ```
 * ① 若在拍 → 先停（列素材前必须停）
 * ② 列相机素材（CameraFile.getFileInfoList → FileInfo.url）
 * ③ 下载到 App 缓存（downloadMediaFile 要 URL + 目录）
 * ④ 转成 MP4：.lrv 走 remux 换壳；.mp4 直接用；其它先试换壳、失败则原样传
 * ⑤ 以「视频原始字节 + Bearer 令牌 + 幂等键」POST 到服务器（出口自动选，见 [MediaUploader]）
 * ```
 *
 * ## 为什么 ③④⑤ 要能「断点续办」
 *
 * 手机连着相机热点时没有公网，而下载又必须在相机热点下做。
 * 于是「下载成功但上传失败」是完全正常的一种结果。
 * 这时把一个已经转好的 mp4 留在缓存里，用户再按一次按钮就**只重试上传**，
 * 不用重新从相机拖一遍（否则每次失败都要重下几十 MB）。
 */
class ClipUploadManager(
    private val context: Context,
    private val camera: CameraController,
) {

    /** 读上传地址与令牌。地址可在界面上改，令牌有默认值。 */
    private val settings = AppSettings(context)

    /** daydreamer 的 job 状态 → 中文。 */
    private val jobStateText = mapOf(
        "uploading" to "上传中",
        "queued" to "等待处理",
        "processing" to "正在生成",
        "ready" to "已完成",
        "failed" to "处理未完成",
        "paused" to "等待恢复",
    )

    interface Listener {
        /** 面向用户的过程消息（会进 App 日志区 + logcat）。 */
        fun onLog(msg: String)

        /** 顶部状态行。 */
        fun onStage(text: String)

        /** 是否正在忙（按钮置灰用）。 */
        fun onBusy(busy: Boolean)

        /** 相机素材列表刷新完成。 */
        fun onMediaList(items: List<MediaItem>) = Unit

        /** 服务器上已有素材的清单，一行一条（含所属文件夹），直接铺到界面上。 */
        fun onServerListRows(rows: List<String>) = Unit
    }

    private var listener: Listener? = null

    /**
     * 专用协程作用域。
     * ⚠️ 和 CameraController 一样，`cancel()` 不可逆，所以 cancel 后必须重建。
     */
    private var scope: CoroutineScope = newScope()

    private var job: Job? = null

    /** 上次「已下载并转好、但上传失败」的文件。存在 → 用户再按按钮时只重试上传。 */
    private var pendingRetry: File? = null

    /** [pendingRetry] 对应的**源素材名**：换了一条素材就不该再复用它。 */
    private var pendingRetrySource: String? = null

    private fun newScope(): CoroutineScope = CoroutineScope(SupervisorJob() + Dispatchers.Main.immediate)

    /** 挂 UI 回调。传 null 表示界面已销毁（解绑，避免泄漏 Activity）。 */
    fun setListener(l: Listener?) {
        listener = l
    }

    fun isBusy(): Boolean = job?.isActive == true

    /** 缓存目录：`cacheDir/heartbeat-upload`。 */
    private fun workDir(): File = File(context.cacheDir, "heartbeat-upload").apply { mkdirs() }

    // ---- 入口 ①：只列素材 ----

    /** 读取相机上的素材列表并回调给界面（不下载、不上传）。 */
    fun refreshMediaList() {
        if (busyGuard()) return
        job = scope.launch {
            listener?.onBusy(true)
            try {
                listener?.onStage("读取相机素材列表…")
                if (!ensureConnected()) return@launch
                // 在拍就先停：拍摄中列素材可能拿不到刚写完的文件，也容易和写入抢锁
                log("⏹ 先停止拍摄（保证文件已落盘）…")
                camera.stopRecord()
                delay(STOP_SETTLE_MS)

                val items = listMediaOrFail() ?: return@launch
                listener?.onStage("素材列表就绪：${items.size} 条")
            } catch (t: Throwable) {
                Timber.w(t, "刷新素材列表异常")
                fail("刷新素材列表异常：${t.message}")
            } finally {
                listener?.onBusy(false)
            }
        }
    }

    // ---- 入口 ②：上传指定素材 ----

    /** 上传用户点选的那一条素材。 */
    fun uploadItem(item: MediaItem, uploadUrl: String) {
        if (busyGuard()) return
        job = scope.launch {
            listener?.onBusy(true)
            try {
                log("🎯 已选中素材：${item.name}（${item.kindLabel}）")
                processAndUpload(item, uploadUrl)
            } catch (t: Throwable) {
                Timber.w(t, "上传链路异常")
                log("❌ 上传链路异常：${t.message}")
                listener?.onStage("上传失败")
            } finally {
                listener?.onBusy(false)
            }
        }
    }

    // ---- 入口 ③：自动挑最新一条（快捷） ----

    /** 自动挑「最新一段视频」上传，并把列表顺带回填给界面。 */
    fun start(uploadUrl: String) {
        if (busyGuard()) return
        job = scope.launch {
            listener?.onBusy(true)
            try {
                if (!ensureConnected()) return@launch
                log("⏹ 先停止拍摄（上传前必须让相机把文件写完）…")
                camera.stopRecord()
                delay(STOP_SETTLE_MS)

                listener?.onStage("读取相机素材列表…")
                val items = listMediaOrFail() ?: return@launch

                // 优先 LRV（体积小、传得快），没有就退到任意视频
                val pick = items.filter { it.isLrv }.maxByOrNull { it.sortKey }
                    ?: items.filter { it.isVideo }.maxByOrNull { it.sortKey }
                if (pick == null) {
                    fail("素材里没有可上传的视频（共 ${items.size} 条，其余为照片或其它类型）。请在上方列表里手动选一条。")
                    return@launch
                }
                log("✅ 自动选中最新素材：${pick.name}（${pick.kindLabel}）")
                processAndUpload(pick, uploadUrl)
            } catch (t: Throwable) {
                Timber.w(t, "上传链路异常")
                log("❌ 上传链路异常：${t.message}")
                listener?.onStage("上传失败")
            } finally {
                listener?.onBusy(false)
            }
        }
    }

    fun cancel() {
        // ⚠️ 只有真的在跑的时候才打「已取消」。
        // 早期版本无条件打这行，而 release() 内部又调 cancel()，
        // 于是 Activity 每重建一次日志区就多一条「已取消上传」——
        // 排查时曾把它误读成「用户点了取消」，白白绕了弯路。
        val wasRunning = job?.isActive == true
        job?.cancel()
        job = null
        listener?.onBusy(false)
        if (wasRunning) {
            log("已取消（已下载的文件仍留在缓存里，下次点上传会优先重试上传）。")
        }
    }

    /** 释放资源。**不打任何用户可见日志**（Activity 重建会频繁走到这里）。 */
    fun release() {
        job?.cancel()
        job = null
        listener?.onBusy(false)
        runCatching { scope.cancel() }
        scope = newScope()
    }

    // ---- 入口 ③：查看「已上传任务」 ----

    /**
     * 查看本机**已上传的任务**（界面里那个「已上传任务」页签）。
     *
     * 命名提示：界面统一叫「已上传任务」，因为 daydreamer 那条路只能查本机传过的任务，
     * 并不是「服务器上所有素材」。只有本地调试服务的 `GET /list` 才是真正的全量列表，
     * 那种情况下日志里仍写「服务器素材」以示区别 —— 两套词汇别混用。
     *
     * 按地址自动分两条路：
     * · `…/api/jobs`（daydreamer）→ [refreshJobsFromServer]，逐条查**已知任务**
     * · `…/upload`（本地调试服务）→ [refreshLegacyList]，一次 `GET /list` 全量
     */
    fun refreshServerList(uploadUrl: String) {
        if (busyGuard()) return
        job = scope.launch {
            listener?.onBusy(true)
            try {
                val trimmed = uploadUrl.trim()
                if (trimmed.isBlank()) {
                    fail("还没填上传地址。请在「素材上传」里填写服务器地址。")
                    return@launch
                }
                if (trimmed.trimEnd('/').endsWith("/api/jobs")) {
                    refreshJobsFromServer(trimmed)
                } else {
                    refreshLegacyList(trimmed)
                }
            } catch (t: Throwable) {
                Timber.w(t, "读取列表异常")
                fail("读取列表异常：${t.message}")
            } finally {
                listener?.onBusy(false)
            }
        }
    }

    /**
     * daydreamer 路线：用受限令牌**逐条查询已知任务**。
     *
     * 为什么不直接 `GET /api/jobs` 拿全量：那个分支要求浏览器 session cookie，
     * 上传凭据不够（实测返回 403「上传凭据仅可上传视频及查询已知任务」）。
     * 契约允许的就是「提交 + 查指定任务」，所以这里只查本机传过的那些。
     */
    private suspend fun refreshJobsFromServer(jobsUrl: String) {
        val ids = settings.uploadedJobIds
        if (ids.isEmpty()) {
            listener?.onServerListRows(
                listOf(
                    "本机还没有上传记录。",
                    "先上传一段素材，「③」就能查到它在这台服务器上的处理进度。",
                )
            )
            listener?.onStage("没有可查询的任务")
            return
        }

        // player_url 是相对路径（形如 /?job=xxx），要拼上服务器根才是可点开的完整地址
        val base = jobsUrl.removeSuffix("/api/jobs").trimEnd('/')
        listener?.onStage("查询 ${ids.size} 个任务…")
        log("☁ 查询本机已上传的 ${ids.size} 个任务")

        val rows = mutableListOf<String>()
        var ready = 0
        var failed = 0
        for (id in ids) {
            val one = MediaUploader(context)
                .fetchText("$jobsUrl/$id", settings.uploadToken)
                .getOrElse { e ->
                    rows += "[查询失败] ${id.take(8)}… — ${e.message}"
                    failed++
                    continue
                }
            val obj = runCatching { org.json.JSONObject(one) }.getOrNull()
            if (obj == null) {
                rows += "[解析失败] ${id.take(8)}…"
                failed++
                continue
            }
            if (obj.optString("state") == "ready") ready++
            appendSingleJob(rows, obj, base)
            rows += ""
        }
        rows += "已完成 $ready / ${ids.size}" + if (failed > 0) "（$failed 条查询失败）" else ""

        listener?.onServerListRows(rows.filter { it.isNotEmpty() || true })
        listener?.onStage("已上传任务已更新")
        log("☁ 已上传任务：$ready 个已完成，$failed 个查询失败")
    }

    /** 本地调试服务路线：一次 `GET /list` 全量列表。 */
    private suspend fun refreshLegacyList(uploadUrl: String) {
        val listUrl = serverListUrl(uploadUrl)
        if (listUrl == null) {
            fail("上传地址不合法，推导不出服务器地址：$uploadUrl")
            return
        }
        listener?.onStage("读取服务器素材…")
        log("☁ 查询服务器素材：GET $listUrl")
        val body = MediaUploader(context)
            .fetchText(listUrl, settings.uploadToken)
            .getOrElse { e ->
                fail(
                    "读取服务器素材失败：${e.message}\n" +
                        "（要能访问到服务器的网络；连着相机热点时请确认手机有蜂窝数据）"
                )
                return
            }
        val rows = formatServerRows(body)
        log("☁ 服务器素材：${rows.size} 行")
        listener?.onServerListRows(rows)
        listener?.onStage("服务器素材已更新")
    }

    // ---- 主流程 ----

    private suspend fun processAndUpload(item: MediaItem, uploadUrl: String) {
        // ⓿ 复用上次转好的文件：避免「上传失败 → 每次都要重下几十 MB」
        //    必须确认是同一条素材，否则用户换了素材却传了上一条，那是更糟的错误。
        val retry = pendingRetry
        if (retry != null && retry.exists() && retry.length() > 0 && pendingRetrySource == item.name) {
            log("↻ 复用缓存里已转好的文件，只重试上传：${retry.name}")
            doUpload(retry, uploadUrl, item.name)
            return
        }

        if (!ensureConnected()) return
        log("ℹ 相机文件服务：${camera.cameraFileEndpointDebug()}")

        log("⏹ 先停止拍摄（上传前必须让相机把文件写完）…")
        camera.stopRecord()
        delay(STOP_SETTLE_MS)

        // ① 下载
        val dir = workDir()
        listener?.onStage("下载中：${item.name}")
        log("⬇ 开始下载：${item.name}\n   url=${item.url}")
        val downloaded = camera.downloadCameraMedia(item.url, dir) { done, total ->
            reportTransfer("下载", done, total)
        }.getOrElse { e ->
            fail(
                "下载失败：${e.message}\n" +
                    "（下载必须连着相机 WiFi；中途别切走 WiFi）"
            )
            return
        }.let { p -> File(p).takeIf { it.isFile && it.length() > 0 } }

        if (downloaded == null) {
            fail("下载完成后本地文件为空。")
            return
        }
        log("✅ 下载完成：${downloaded.name}（${mb(downloaded.length())}）")

        // ② 转 MP4
        listener?.onStage("转换中 → MP4")
        val stem = downloaded.name.substringBeforeLast('.')
        val ext = downloaded.extension.lowercase()
        val converted = withContext(Dispatchers.IO) {
            when (ext) {
                "lrv" -> LrvToMp4Converter.convert(downloaded, File(dir, "$stem.mp4"))
                // 本来就是 MP4/MOV：直接用。
                // ⚠️ 别再做 copyTo —— 目标名与源名可能完全相同，那是「源=目标」的自拷贝，会把文件清空。
                "mp4", "mov" -> Result.success(LrvToMp4Converter.ConvertResult(downloaded, remuxed = false))
                else -> {
                    // 全景(insv/insp)或照片：先试着换壳，失败就原样上传
                    val out = File(dir, "$stem.mp4")
                    LrvToMp4Converter.convert(downloaded, out)
                        .recoverCatching { LrvToMp4Converter.ConvertResult(downloaded, remuxed = false) }
                }
            }
        }.getOrElse { e ->
            fail("转换 MP4 失败：${e.message}")
            return
        }
        log(
            when {
                ext == "mp4" || ext == "mov" ->
                    "✅ 待上传文件就绪（本来就是 MP4，无需转换）：${converted.file.name}（${mb(converted.file.length())}）"
                converted.remuxed ->
                    "✅ 已转成标准 MP4（remux 换壳，未重新编码，画质无损）：${converted.file.name}（${mb(converted.file.length())}）"
                else ->
                    "✅ 已输出文件（复制换扩展名 —— 源容器未被识别为标准 MP4）：${converted.file.name}（${mb(converted.file.length())}）"
            }
        )

        // ③ 上传
        pendingRetry = converted.file
        pendingRetrySource = item.name
        doUpload(converted.file, uploadUrl, item.name)
    }

    /**
     * 上传一个已经准备好的文件。
     *
     * @param sourceName **相机上的原始素材名**（如 `LRV_20260923_160523_01_093.lrv`），
     *   用来推导幂等键。必须用源素材名而不是本地 mp4 的临时名 ——
     * 这样「同一段录像无论转码多少次、重试多少轮」都对应同一个键，服务端才能正确去重。
     */
    private suspend fun doUpload(file: File, uploadUrl: String, sourceName: String) {
        if (uploadUrl.isBlank()) {
            fail("还没填上传地址。请在「素材上传」里填写服务器地址，例如 https://your-server.example.com/api/jobs")
            return
        }
        val token = settings.uploadToken
        val idemKey = idempotencyKeyFor(sourceName)

        listener?.onStage("上传中：${file.name}")
        log("⬆ 开始上传到 $uploadUrl（${file.name}，${mb(file.length())}）")
        log(
            "   鉴权=${if (token.isBlank()) "未配置令牌" else "Bearer ${token.take(6)}…"}" +
                "　幂等键=${idemKey.take(8)}…（重试复用同一个，服务端不会重复建任务）"
        )
        val started = System.currentTimeMillis()

        val res = MediaUploader(context).upload(
            file = file,
            url = uploadUrl,
            uploadToken = token,
            idempotencyKey = idemKey,
            onProgress = { done, total -> reportTransfer("上传", done, total) },
        ).getOrElse { e ->
            fail(
                "上传失败：${e.message}\n" +
                    "提示：手机连着相机热点时没有公网，App 会优先改用蜂窝数据上传；" +
                    "若手机没插卡/没开流量，请先断开相机 WiFi（文件已缓存在手机里），再点一次按钮即可续传。\n" +
                    "若报 certificate / SSL 类错误，说明服务器 HTTPS 证书过期或不被本机信任。"
            )
            return
        }

        val cost = (System.currentTimeMillis() - started) / 1000.0
        log("🎉 上传成功（HTTP ${res.httpCode}，出口：${res.networkLabel}，耗时 ${"%.1f".format(cost)}s）")

        // 记下 job id —— 受限的上传凭据只能查「已知任务」，
        // 不存下来的话，这段视频在服务器上的处理进度就再也查不到了。
        val jobId = runCatching { org.json.JSONObject(res.body).optString("id", "") }.getOrDefault("")
        if (jobId.isNotBlank()) {
            settings.rememberJobId(jobId)
            log("   任务 ${jobId.take(8)}… 已提交，稍后点「③」可查它的处理进度")
        }
        if (res.body.isNotBlank()) log("   服务器返回：${res.body}")
        Timber.i("clip uploaded: %s -> %s", file.name, uploadUrl)

        // 成功后才清缓存；失败时故意留着，供下次「只重试上传」
        pendingRetry = null
        pendingRetrySource = null
        runCatching { file.delete() }
        runCatching { File(workDir(), file.name.replace(".mp4", ".lrv")).delete() }
        listener?.onStage("上传完成 ✓")
    }

    /**
     * 由源素材名推导幂等键：32 位小写 hex，与 daydreamer 前端的 `Idempotency-Key` 同格式。
     *
     * 用 `nameUUIDFromBytes`（确定性）而**不是**随机 UUID。契约要求
     * 「One UUID per recording; reuse it for upload retries」—— 随机会让每次重传
     * 都被服务端当成新任务，白跑一遍生成流程。
     */
    private fun idempotencyKeyFor(sourceName: String): String =
        java.util.UUID.nameUUIDFromBytes("heartbeat-camera:$sourceName".toByteArray())
            .toString()
            .replace("-", "")

    // ---- 工具 ----

    /**
     * 推导「素材清单」URL。
     *
     * · 地址是 daydreamer 的 `…/api/jobs` → **GET 同一个地址**就是任务清单（服务端同一路径分 POST/GET）
     * · 地址是本地调试服务 `…/upload` → 换成同源的 `/list`
     *
     * 解析不出来返回 null。
     */
    private fun serverListUrl(uploadUrl: String): String? {
        val trimmed = uploadUrl.trim()
        if (trimmed.isBlank()) return null
        return runCatching {
            if (trimmed.trimEnd('/').endsWith("/api/jobs")) return@runCatching trimmed
            val u = java.net.URL(trimmed)
            val port = if (u.port > 0) ":${u.port}" else ""
            "${u.protocol}://${u.host}$port/list"
        }.getOrNull()
    }

    /**
     * 把服务端返回的 JSON 排成多行文本。支持两种格式：
     *
     * 1. **daydreamer 的任务清单**（`GET /api/jobs`）：
     *    `{"jobs":[{"id":"…","state":"ready","message":"…","created":1758…,"player_url":"…"}]}`
     *    —— 当前默认格式，一段录像 = 一个 job。
     * 2. 本地调试服务的文件清单（`GET /list`）：`{"totalCount":2,"groups":[…]}`。
     *
     * 解析失败不抛异常 —— 退化成把原始响应贴出来，至少让用户看到服务端说了什么。
     */
    private fun formatServerRows(json: String): List<String> {
        val rows = mutableListOf<String>()
        runCatching {
            val o = org.json.JSONObject(json)

            // ① daydreamer 任务清单
            val jobs = o.optJSONArray("jobs")
            if (jobs != null) {
                appendJobRows(rows, jobs)
                return rows
            }

            // ② 本地调试服务的文件清单
            val total = o.optInt("totalCount", 0)
            val sizeText = o.optString("totalSizeText", "?")
            val rootPath = o.optString("root", "")

            if (total <= 0) {
                rows += "服务器上还没有视频。"
                if (rootPath.isNotBlank()) rows += "存放目录：$rootPath"
                return rows
            }

            rows += "共 $total 个文件 · $sizeText"
            if (rootPath.isNotBlank()) rows += "服务器目录：$rootPath"

            val groups = o.optJSONArray("groups") ?: org.json.JSONArray()
            for (i in 0 until groups.length()) {
                val g = groups.optJSONObject(i) ?: continue
                val files = g.optJSONArray("files") ?: org.json.JSONArray()
                rows += "📁 ${g.optString("folder", "?")}（${g.optInt("count", files.length())} 个文件）"
                for (j in 0 until files.length()) {
                    val f = files.optJSONObject(j) ?: continue
                    rows += "　· ${f.optString("name")} · ${f.optString("sizeText")} · ${f.optString("mtime")}"
                }
            }
        }.onFailure { e ->
            rows += "解析服务器返回失败：${e.message}"
            rows += json.take(400)
        }
        return rows
    }

    /**
     * daydreamer 的任务清单：**一段录像 = 一个 job**。
     *
     * `state` 依次是 uploading → queued → processing → ready；
     * 失败会停在 failed（服务端支持 POST /api/jobs/{id}/resume 恢复）。
     * 这里把英文状态翻成中文，并把 `created`（秒级时间戳）排成可读时间。
     */
    private fun appendJobRows(rows: MutableList<String>, jobs: org.json.JSONArray) {
        if (jobs.length() == 0) {
            rows += "服务器上还没有任务。"
            rows += "传上去的视频会在这里显示处理进度。"
            return
        }

        val stateText = jobStateText

        var ready = 0
        rows += "共 ${jobs.length()} 个任务"
        for (i in 0 until jobs.length()) {
            val j = jobs.optJSONObject(i) ?: continue
            val state = j.optString("state", "?")
            if (state == "ready") ready++

            rows += "· [${stateText[state] ?: state}] ${j.optString("message", "")}"

            val created = j.optLong("created", 0L)
            val whenText = if (created > 0) {
                SimpleDateFormat("MM-dd HH:mm", Locale.CHINA).format(Date(created * 1000))
            } else {
                ""
            }
            val player = j.optString("player_url", "")
            val tail = buildString {
                if (whenText.isNotBlank()) append(whenText)
                if (player.isNotBlank()) {
                    if (isNotEmpty()) append("　·　")
                    append("可播放 $player")
                }
            }
            if (tail.isNotBlank()) rows += "　　$tail"
        }
        rows += "已完成 $ready / ${jobs.length()}"
    }

    /**
     * 单个任务的详情行（「③ 查看已上传任务」逐条查询时用）。
     *
     * ⚠️ `player_url` 是**相对路径**（形如 `/?job=xxx`），
     * 必须拼上服务器根地址才是能交给用户打开的完整链接。
     */
    private fun appendSingleJob(rows: MutableList<String>, j: org.json.JSONObject, base: String) {
        val state = j.optString("state", "?")
        val id = j.optString("id", "")

        rows += "[${jobStateText[state] ?: state}] ${j.optString("message", "")}"

        val created = j.optLong("created", 0L)
        if (created > 0) {
            val whenText = SimpleDateFormat("MM-dd HH:mm:ss", Locale.CHINA).format(Date(created * 1000))
            rows += "　　提交于 $whenText"
        }
        rows += "　　任务号 ${id.take(8)}…"

        val video = j.optString("video_url", "")
        if (video.isNotBlank()) {
            rows += "　　视频 " + if (video.startsWith("http")) video else "$base$video"
        }
        val player = j.optString("player_url", "")
        if (player.isNotBlank()) {
            rows += "　　播放页 " + if (player.startsWith("http")) player else "$base$player"
        }
    }

    /** 统一的「已有任务在跑」提示。返回 true 表示应当中止新任务。 */
    private fun busyGuard(): Boolean {
        if (job?.isActive == true) {
            log("⏳ 上一个任务还在进行中，请等它结束（或点「取消」）。")
            return true
        }
        return false
    }

    private suspend fun ensureConnected(): Boolean {
        if (!camera.isConnected()) {
            fail("相机未连接。请先在系统 WiFi 里连上相机热点，再回 App 点「连接相机」。（下载素材必须走 WiFi）")
            return false
        }
        return true
    }

    /** 列素材 + 回填 UI；失败返回 null 并已打日志。 */
    private suspend fun listMediaOrFail(): List<MediaItem>? {
        val items = camera.listCameraMediaItems().getOrElse { e ->
            fail("读取相机素材失败：${e.message}")
            return null
        }
        if (items.isEmpty()) return null
        val videos = items.count { it.isVideo }
        log("📄 素材共 ${items.size} 条：视频 $videos 条、LRV ${items.count { it.isLrv }} 条")
        log(items.take(LIST_PREVIEW).joinToString("\n") { "   [${it.kindLabel}] ${it.name}" })
        if (items.size > LIST_PREVIEW) log("   …（其余 ${items.size - LIST_PREVIEW} 条见下方列表）")
        listener?.onMediaList(items)
        return items
    }

    private var lastTickAt = 0L

    /** 传输进度节流上报：64KB 一个回调太密，日志区会被刷爆。 */
    private fun reportTransfer(tag: String, done: Long, total: Long) {
        val now = System.currentTimeMillis()
        val finished = total > 0 && done >= total
        if (!finished && now - lastTickAt < PROGRESS_TICK_MS) return
        lastTickAt = now
        val pct = if (total > 0) done * 100 / total else 0
        listener?.onStage("$tag ${mb(done)} / ${mb(total)}（$pct%）")
        if (finished || pct % 25 == 0L) {
            log("   $tag 进度 $pct%（${mb(done)} / ${mb(total)}）")
        }
    }

    private fun fail(msg: String) {
        log("❌ $msg")
        listener?.onStage("失败")
        Timber.w("clip upload failed: %s", msg)
    }

    private fun log(msg: String) {
        listener?.onLog(msg)
    }

    private fun mb(bytes: Long): String = "%.2f MB".format(bytes / 1048576.0)

    private companion object {
        /** 停拍后等相机把文件写完 / 松开写锁。 */
        const val STOP_SETTLE_MS = 1_500L

        /** 进度节流间隔。 */
        const val PROGRESS_TICK_MS = 400L

        /** 日志区里最多预览几条素材（其余在界面列表里看）。 */
        const val LIST_PREVIEW = 6
    }
}
