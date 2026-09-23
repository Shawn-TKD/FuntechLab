package com.insta360.heartbeat

import android.app.Application
import androidx.appcompat.app.AppCompatDelegate
import com.arashivision.sdk.camera.InstaCameraSDK
import com.insta360.heartbeat.settings.AppSettings
import timber.log.Timber

/**
 * SDK 初始化总开关。权限授予后调用 [initWithPermissionsGranted]。
 * 参考 Insta360 官方 demo 的 MainActivity.initSDK()。
 *
 * 注：本工程只做「实时触发拍摄」，不做事后文件同步，因此不引入 sdk-media
 * （它的 bmgmedia 传递依赖会带进约 155MB 原生库，见 app/build.gradle.kts 说明）。
 */
class HeartbeatApp : Application() {

    override fun onCreate() {
        super.onCreate()
        instance = this
        Timber.plant(Timber.DebugTree())

        // 主题：V15 默认深色（户外取景时屏幕反射更少）。
        // ⚠️ 必须在 super.onCreate 之后、任何 Activity 创建之前设置，
        // 否则第一帧会用错配置，出现「启动闪一下白底」。
        val light = runCatching { AppSettings(this).lightTheme }.getOrDefault(false)
        AppCompatDelegate.setDefaultNightMode(
            if (light) AppCompatDelegate.MODE_NIGHT_NO else AppCompatDelegate.MODE_NIGHT_YES
        )

        // 真机测试常无 adb，把崩溃堆栈落盘，便于事后排查（见 CrashLogger 注释）
        CrashLogger.install(this)
    }

    /** 在所有运行时权限被授予后调用（见 MainActivity）。 */
    fun initWithPermissionsGranted() {
        if (sdkInitialized) return
        InstaCameraSDK.init(this) {
            cacheDir = externalCacheDir?.absolutePath
        }
        sdkInitialized = true
    }

    companion object {
        lateinit var instance: HeartbeatApp
            private set

        var sdkInitialized: Boolean = false
            private set
    }
}
