package com.insta360.heartbeat.upload

import android.media.MediaCodec
import android.media.MediaExtractor
import android.media.MediaFormat
import android.media.MediaMuxer
import java.io.File
import java.nio.ByteBuffer
import timber.log.Timber

/**
 * 把相机里的 `.lrv` 变成标准 `.mp4`。
 *
 * ## 为什么不是「转码」
 *
 * Insta360 的 LRV 是**给手机端做快速预览用的低码率副本**，它本身已经是
 * H.264/H.265 + AAC 的 ISO-BMFF 流，只是扩展名不是 `.mp4`。
 * 所以这里能做的是 **remux（换壳）**：把音视频轨原样搬进新的 MP4 容器，
 * —— 不重新编码 = 快（秒级）+ 无损 + 不吃 CPU。
 *
 * ## 两级策略
 *
 * 1. **原地换壳**：`MediaExtractor` 能读出轨道 → `MediaMuxer` 写标准 MP4。
 *    这一步能修掉「扩展名不对导致播放器/服务器不认」的问题，输出是真正规范的 MP4。
 * 2. **兜底：纯字节复制 + 改扩展名**。
 *    万一固件写出的 LRV 头不够规范（Extractor 读不出来），直接拷一份改名。
 *    多数播放器照样能播，至少**不会因为转换失败而让整条上传链路走不下去**。
 *
 * 返回的 [Result] 里还带一个 [ConvertResult.remuxed] 标记，用于在日志里说清用的是哪条路。
 */
object LrvToMp4Converter {

    data class ConvertResult(
        val file: File,
        /** true = 真的重写了 MP4 容器；false = 只是复制改名。 */
        val remuxed: Boolean,
    )

    /**
     * @param source 已下载到本地的 `.lrv`（任何扩展名都能吃）
     * @param target 输出的 `.mp4`
     */
    fun convert(source: File, target: File): Result<ConvertResult> = runCatching {
        require(source.exists()) { "源文件不存在：${source.absolutePath}" }
        require(source.length() > 0) { "源文件是空的（可能是下载中断）：${source.absolutePath}" }
        target.parentFile?.mkdirs()
        if (target.exists()) target.delete()

        val remuxed = remux(source, target)
        if (remuxed != null) {
            Timber.i("LRV→MP4 remux 成功：%d 字节", remuxed.length())
            return@runCatching ConvertResult(remuxed, remuxed = true)
        }

        Timber.w("LRV→MP4 remux 不可用，退回「复制改名」")
        source.copyTo(target, overwrite = true)
        ConvertResult(target, remuxed = false)
    }

    /** 成功返回输出文件，失败/不适用返回 null（由调用方决定兜底）。 */
    private fun remux(input: File, output: File): File? {
        val extractor = MediaExtractor()
        var muxer: MediaMuxer? = null
        return try {
            extractor.setDataSource(input.absolutePath)
            val trackCount = extractor.trackCount
            if (trackCount <= 0) {
                null
            } else {
                val mx = MediaMuxer(output.absolutePath, MediaMuxer.OutputFormat.MUXER_OUTPUT_MPEG_4)
                muxer = mx
                val trackMap = HashMap<Int, Int>()
                var bufferSize = DEFAULT_BUFFER_SIZE

                for (i in 0 until trackCount) {
                    val format = extractor.getTrackFormat(i)
                    val mime = format.getString(MediaFormat.KEY_MIME) ?: continue
                    // 只搬音视频轨；LRV 里可能还有相机私有的元数据轨，搬进 MP4 会让播放器报错
                    if (!mime.startsWith("video/") && !mime.startsWith("audio/")) continue
                    extractor.selectTrack(i)
                    trackMap[i] = mx.addTrack(format)
                    if (format.containsKey(MediaFormat.KEY_MAX_INPUT_SIZE)) {
                        bufferSize = maxOf(bufferSize, format.getInteger(MediaFormat.KEY_MAX_INPUT_SIZE))
                    }
                }

                if (trackMap.isEmpty()) {
                    mx.release()
                    muxer = null
                    null
                } else {
                    mx.start()
                    val buffer = ByteBuffer.allocate(bufferSize)
                    val info = MediaCodec.BufferInfo()
                    var samples = 0L

                    while (true) {
                        val size = extractor.readSampleData(buffer, 0)
                        if (size < 0) break
                        val outTrack = trackMap[extractor.sampleTrackIndex]
                        if (outTrack == null) {
                            // 该轨没被选中（元数据轨）→ 跳过这个样本继续
                            extractor.advance()
                            continue
                        }
                        info.offset = 0
                        info.size = size
                        info.presentationTimeUs = extractor.sampleTime
                        val sync = extractor.sampleFlags and MediaExtractor.SAMPLE_FLAG_SYNC != 0
                        info.flags = if (sync) MediaCodec.BUFFER_FLAG_KEY_FRAME else 0
                        mx.writeSampleData(outTrack, buffer, info)
                        samples++
                        extractor.advance()
                    }

                    mx.stop()
                    mx.release()
                    muxer = null
                    Timber.i("LRV remux：轨道 %d 个，样本 %d 个", trackMap.size, samples)
                    if (output.length() > 0) output else null
                }
            }
        } catch (t: Throwable) {
            Timber.w(t, "LRV remux 失败，将走复制改名兜底")
            runCatching { muxer?.release() }
            null
        } finally {
            runCatching { extractor.release() }
        }
    }

    private const val DEFAULT_BUFFER_SIZE = 1 shl 20
}
