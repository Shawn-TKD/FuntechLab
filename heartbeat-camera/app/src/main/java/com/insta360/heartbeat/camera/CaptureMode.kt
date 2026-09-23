package com.insta360.heartbeat.camera

import com.arashivision.sdk.camera.core.model.FunctionMode

/**
 * App 上可选的拍摄模式。
 *
 * 为什么只有两种：本工程是「心率触发」场景，只需要「心率高时持续记录」这一件事。
 * SDK 支持 25 种 FunctionMode，但其余（子弹时间/星空延时/移动延时等）在本场景无意义。
 *
 * ⚠️ 关键事实（从 GO Ultra 固件配置文件 `assets/insta/json/params/go_ultra.json` 实证）：
 * GO Ultra **原生支持间隔拍照** `PHOTO_INTERVAL`，带 `lapse_time` 参数，
 * 合法值 `[0, 1, 3, 5, 10, 30, 60, 120]` 秒。
 * 也就是说「每隔 N 秒拍一张」是**相机固件自己完成的**，
 * App 只需下发一次 `startCapture()`，不需要在手机侧跑定时器反复触发
 * （那样既费电、又会因为 BLE/WiFi 往返延迟导致间隔不准）。
 */
enum class CaptureMode(
    /** 对应的 SDK 拍摄模式。 */
    val functionMode: FunctionMode,
    /** UI 上显示的名字。 */
    val label: String,
) {
    /** 录像：心率达阈值开始录，回落停止。 */
    VIDEO(FunctionMode.VIDEO_NORMAL, "录像"),

    /** 拍照：心率达阈值开始间隔拍照，回落停止。 */
    PHOTO(FunctionMode.PHOTO_INTERVAL, "拍照（间隔）"),
    ;

    /** 该模式是否需要「间隔秒数」参数。 */
    val usesInterval: Boolean get() = this == PHOTO

    companion object {
        fun fromName(name: String?): CaptureMode =
            entries.firstOrNull { it.name == name } ?: VIDEO
    }
}
