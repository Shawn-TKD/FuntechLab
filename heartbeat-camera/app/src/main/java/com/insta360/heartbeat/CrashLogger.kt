package com.insta360.heartbeat

import android.content.Context
import android.os.Build
import java.io.File
import java.io.PrintWriter
import java.io.StringWriter
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/**
 * 全局未捕获异常记录器。
 *
 * 为什么需要它：
 *  真机测试时手机往往**没有连 adb**，闪退后拿不到 logcat，只能靠猜。
 *  这里把崩溃堆栈写到「外部私有目录」下的 crash.txt，
 *  路径形如 /sdcard/Android/data/com.insta360.heartbeat/files/crash.txt，
 *  用手机自带的「文件管理」App 就能直接打开查看，无需 root、无需数据线。
 *
 * 设计上**极其保守**：所有写文件动作都包在 runCatching 里，
 * 保证「记录崩溃」这件事本身绝不会再引发一次崩溃。
 */
object CrashLogger {

    private const val FILE_NAME = "crash.txt"

    private var defaultHandler: Thread.UncaughtExceptionHandler? = null

    fun install(context: Context) {
        defaultHandler = Thread.getDefaultUncaughtExceptionHandler()
        val appContext = context.applicationContext
        // 用 SAM 转换构造处理器（不实现接口，避免 object 声明必须显式 override 的问题）
        val handler = Thread.UncaughtExceptionHandler { thread, throwable ->
            runCatching { write(appContext, thread, throwable) }
            // 交回系统默认处理，保持正常的崩溃行为（进程退出、系统弹「已停止运行」）
            defaultHandler?.uncaughtException(thread, throwable)
        }
        Thread.setDefaultUncaughtExceptionHandler(handler)
    }

    private fun write(context: Context, thread: Thread, throwable: Throwable) {
        val sw = StringWriter()
        throwable.printStackTrace(PrintWriter(sw))
        val time = SimpleDateFormat("yyyy-MM-dd HH:mm:ss", Locale.US).format(Date())
        val text = buildString {
            appendLine("===== 崩溃报告 =====")
            appendLine("时间: $time")
            appendLine("线程: ${thread.name}")
            appendLine("机型: ${Build.MANUFACTURER} ${Build.MODEL}")
            appendLine("Android: ${Build.VERSION.RELEASE} (API ${Build.VERSION.SDK_INT})")
            appendLine("App: ${BuildConfig.APPLICATION_ID} v${BuildConfig.VERSION_NAME}")
            appendLine("---------------------")
            appendLine(sw.toString())
        }

        // 优先写外部私有目录（文件管理器可见）；失败则退回内部私有目录
        val target =
            (context.getExternalFilesDir(null) ?: context.filesDir).resolve(FILE_NAME)
        target.parentFile?.mkdirs()
        // 追加而非覆盖，便于记录多次崩溃
        target.appendText(text + "\n\n")
    }

    /** 供 UI 展示：读取上次崩溃记录（若有）。 */
    fun readLast(context: Context): String? {
        val target =
            (context.getExternalFilesDir(null) ?: context.filesDir).resolve(FILE_NAME)
        return runCatching { if (target.exists()) target.readText() else null }.getOrNull()
    }
}
