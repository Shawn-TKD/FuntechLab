package com.insta360.heartbeat.upload

import android.content.Context
import android.net.ConnectivityManager
import android.net.Network
import android.net.NetworkCapabilities
import java.io.BufferedOutputStream
import java.io.File
import java.io.IOException
import java.net.HttpURLConnection
import java.net.URL
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import timber.log.Timber

/**
 * 把本地文件以 `multipart/form-data` 上传到 HTTP 服务器。
 *
 * ## ⚠️ 这个类里最重要的一件事：**上传必须挑对网络出口**
 *
 * 真实场景是：手机为了控制 / 下载相机素材，**必须连在 GO Ultra 的 WiFi 热点上**，
 * 而那个热点是没有公网出口的。此时如果直接用默认网络发 HTTP，请求会打到相机热点上，
 * 必然超时 —— 这就是「连着相机就没法上传」的根因。
 *
 * 解法：发请求前先判断「当前默认网络通不通公网」——
 *  · 默认网络是 **validated（已验证联网）** 的 → 就用它（普通家里 WiFi 场景，服务器在局域网里也走它）；
 *  · 默认网络**没有联网验证**（说明正卡在相机热点上）→ 显式挑一个带 `NET_CAPABILITY_VALIDATED`
 *    的网络（优先蜂窝）来发请求，用 [Network.openConnection] 把 socket 绑到那个出口上。
 *    这样**手机可以一边连着相机热点，一边用 4G/5G 把视频传上服务器**。
 *
 * 回环地址（`127.0.0.1`）不绑定网络 —— 那是给 `adb reverse` 调试通道用的。
 */
class MediaUploader(private val context: Context) {

    data class Result(
        val httpCode: Int,
        /** 服务端返回的响应体（截断到 [MAX_BODY_CHARS]）。 */
        val body: String,
        val networkLabel: String,
    )

    /**
     * 把本地文件以**视频原始字节体**（raw body）POST 到服务器。
     *
     * ## 协议：对齐白日梦想家（daydreamer）的 `/api/jobs`
     *
     * 这套协议是照抄它的前端参考实现（`/opt/daydreamer/web/upload.js`）来的：
     *  · 请求体 = **文件字节本身**，不是 `multipart/form-data`
     *  · `Content-Type` 按扩展名判定（服务端只认 mp4/mov/webm/mkv/3gp）
     *  · `Authorization: Bearer <uploadToken>` —— 服务端 `hmac.compare_digest` 比对，
     *    与浏览器的「连接码」session 相互独立，手机端不需要先换 cookie
     *  · `Idempotency-Key: <32 位 hex>` —— **同一段素材重试必须复用同一个 key**，
     *    服务端据此去重：网络抖动重传不会重复建生成任务
     *
     * @param file 要上传的文件
     * @param url 服务器接收地址（`https://host/api/jobs`）
     * @param contentType 请求的 Content-Type，默认按扩展名推断
     * @param uploadToken 鉴权令牌；空串表示不带 Authorization 头（本地调试服务用）
     * @param idempotencyKey 幂等键；空串表示不带该头
     * @param onProgress (已发送字节, 总字节)
     */
    suspend fun upload(
        file: File,
        url: String,
        contentType: String = guessMime(file.name),
        uploadToken: String = "",
        idempotencyKey: String = "",
        onProgress: (Long, Long) -> Unit = { _, _ -> },
    ): kotlin.Result<Result> = withContext(Dispatchers.IO) {
        runCatching {
            require(file.exists()) { "文件不存在：${file.absolutePath}" }
            require(file.length() > 0) { "文件为空：${file.absolutePath}" }

            val (network, label) = pickUploadNetwork(url)
            Timber.i("上传出口：%s（文件 %.1f MB）", label, file.length() / 1048576.0)

            val total = file.length()
            val conn = (network?.openConnection(URL(url))
                ?: URL(url).openConnection()) as HttpURLConnection

            conn.requestMethod = "POST"
            conn.doOutput = true
            conn.connectTimeout = CONNECT_TIMEOUT_MS
            conn.readTimeout = READ_TIMEOUT_MS
            conn.setRequestProperty("Content-Type", contentType)
            conn.setRequestProperty("Accept", "application/json")
            if (uploadToken.isNotBlank()) {
                conn.setRequestProperty("Authorization", "Bearer $uploadToken")
            }
            if (idempotencyKey.isNotBlank()) {
                conn.setRequestProperty("Idempotency-Key", idempotencyKey)
            }
            // 定长流有两个作用：① HttpURLConnection 不会把整个文件缓冲进内存（几十 MB 会 OOM）；
            // ② 进度回调能拿到真实总量（用 chunked 就没有总长可报）。
            conn.setFixedLengthStreamingMode(total)

            var sent = 0L
            try {
                BufferedOutputStream(conn.outputStream, BUFFER_SIZE).use { out ->
                    val buf = ByteArray(BUFFER_SIZE)
                    file.inputStream().use { input ->
                        while (true) {
                            val n = input.read(buf)
                            if (n <= 0) break
                            out.write(buf, 0, n)
                            sent += n
                            onProgress(sent, total)
                        }
                    }
                    out.flush()
                }

                val code = conn.responseCode
                val stream = if (code in 200..299) conn.inputStream else conn.errorStream
                val body = stream?.bufferedReader()?.use { it.readText() }.orEmpty()
                val trimmed = if (body.length > MAX_BODY_CHARS) body.take(MAX_BODY_CHARS) + "…" else body

                if (code in 200..299) {
                    Result(code, trimmed, label)
                } else {
                    throw IOException("服务器返回 HTTP $code：$trimmed")
                }
            } finally {
                runCatching { conn.disconnect() }
            }
        }
    }

    /**
     * 向服务器发一个 GET，取回响应体（「查看已上传任务」用）。
     *
     * 复用 [pickUploadNetwork]：连着相机热点时也能自动改走蜂窝把清单拉回来。
     *
     * `uploadToken` 非空时带上 `Authorization: Bearer` —— daydreamer 的
     * `GET /api/jobs`（查任务清单）和 POST 走同一套鉴权。
     */
    suspend fun fetchText(url: String, uploadToken: String = ""): kotlin.Result<String> =
        withContext(Dispatchers.IO) {
        runCatching {
            val (network, label) = pickUploadNetwork(url)
            Timber.i("拉取清单出口：%s -> %s", label, url)

            val conn = (network?.openConnection(URL(url))
                ?: URL(url).openConnection()) as HttpURLConnection

            conn.requestMethod = "GET"
            conn.connectTimeout = CONNECT_TIMEOUT_MS
            conn.readTimeout = READ_TIMEOUT_MS
            conn.setRequestProperty("Accept", "application/json, text/html")
            if (uploadToken.isNotBlank()) {
                conn.setRequestProperty("Authorization", "Bearer $uploadToken")
            }

            try {
                val code = conn.responseCode
                val stream = if (code in 200..299) conn.inputStream else conn.errorStream
                val body = stream?.bufferedReader()?.use { it.readText() }.orEmpty()
                if (code !in 200..299) {
                    throw IOException("服务器返回 HTTP $code：${body.take(MAX_BODY_CHARS)}")
                }
                body
            } finally {
                runCatching { conn.disconnect() }
            }
        }
    }

    /**
     * 选上传出口。返回 `null` 表示「用系统默认网络」。
     *
     * 判定顺序见类注释：默认网络已验证联网 → 直接用；否则找一个已验证的网络绑上去。
     *
     * 用 `allNetworks` 而不是 `registerNetworkCallback`：这里要的是「**发请求前同步问一次**」，
     * 用回调去跟踪网络集合会引入一个全局有状态的对象，为一个 HTTP 请求不值得。
     */
    @Suppress("DEPRECATION") // allNetworks 已 deprecated，但同步枚举正是这里需要的（见下方注释）
    private fun pickUploadNetwork(url: String): Pair<Network?, String> {
        val cm = context.getSystemService(ConnectivityManager::class.java)
            ?: return null to "默认网络（无 ConnectivityManager）"

        val host = runCatching { URL(url).host }.getOrNull().orEmpty()
        if (isLoopback(host)) return null to "默认网络（回环地址，供 adb reverse 调试）"

        fun caps(n: Network): NetworkCapabilities? = runCatching { cm.getNetworkCapabilities(n) }.getOrNull()

        val active = runCatching { cm.activeNetwork }.getOrNull()
        val activeCaps = active?.let { caps(it) }
        if (activeCaps != null && activeCaps.hasCapability(NetworkCapabilities.NET_CAPABILITY_VALIDATED)) {
            val kind = if (activeCaps.hasTransport(NetworkCapabilities.TRANSPORT_WIFI)) "WiFi" else "非WiFi"
            return null to "系统默认网络（$kind，已验证联网）"
        }

        val nets = runCatching { cm.allNetworks }.getOrNull().orEmpty()
        val validated = nets.filter { n ->
            val c = caps(n) ?: return@filter false
            c.hasCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET) &&
                c.hasCapability(NetworkCapabilities.NET_CAPABILITY_VALIDATED)
        }
        // 优先蜂窝：连着相机热点时，蜂窝通常是唯一真正能上公网的出口
        val cellular = validated.firstOrNull {
            caps(it)?.hasTransport(NetworkCapabilities.TRANSPORT_CELLULAR) == true
        }
        val chosen = cellular ?: validated.firstOrNull()
        return if (chosen != null) {
            val kind = if (cellular != null) "蜂窝数据" else "其它已验证网络"
            chosen to "$kind（显式绑定，绕过无公网的相机热点）"
        } else {
            null to "默认网络（⚠ 未找到可用公网出口：可能没插卡/没开流量，上传大概率失败）"
        }
    }

    private fun isLoopback(host: String): Boolean =
        host == "127.0.0.1" || host == "localhost" || host == "::1" || host == "0.0.0.0"

    /**
     * 按扩展名推 Content-Type。
     *
     * ⚠️ 这张表要和 daydreamer 前端（`upload.js` 里的 `types`）保持一致 ——
     * 服务端按扩展名判类型，映射不上就会被拒。
     * `.lrv` 归到 `video/mp4`：LRV 本身就是 MP4 容器，且转码产物就是 mp4。
     */
    private fun guessMime(name: String): String = when {
        name.endsWith(".mp4", true) -> "video/mp4"
        name.endsWith(".mov", true) -> "video/quicktime"
        name.endsWith(".webm", true) -> "video/webm"
        name.endsWith(".mkv", true) -> "video/x-matroska"
        name.endsWith(".3gp", true) -> "video/3gpp"
        name.endsWith(".lrv", true) -> "video/mp4"
        name.endsWith(".jpg", true) || name.endsWith(".jpeg", true) -> "image/jpeg"
        else -> "application/octet-stream"
    }

    private companion object {
        const val CONNECT_TIMEOUT_MS = 15_000
        const val READ_TIMEOUT_MS = 180_000
        const val BUFFER_SIZE = 64 * 1024
        const val MAX_BODY_CHARS = 512
    }
}
