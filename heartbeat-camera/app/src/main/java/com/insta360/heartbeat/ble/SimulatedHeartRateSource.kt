package com.insta360.heartbeat.ble

import android.os.Handler
import android.os.Looper
import kotlin.math.sin

/**
 * 模拟心率源：没有任何真实心率设备时，周期性抛出模拟心率曲线。
 *
 * 用途：
 *  - 离线开发 / 验证「阈值引擎 + 相机控制」全链路，无需 Garmin FR255 也能跑通。
 *  - 曲线设计：心率先低于停止阈值 → 约 8 秒后升到开拍阈值 → 维持一会 → 再回落。
 *    模拟峰值和低谷由调用方根据用户设置传入，避免用户把阈值改高后模拟永远触发不了。
 */
class SimulatedHeartRateSource {

    interface Listener {
        fun onSimulatedHeartRate(hr: Int)
    }

    private val handler = Handler(Looper.getMainLooper())
    private var running = false
    private var tick = 0
    private var onThreshold = 110
    private var offThreshold = 100

    /** 更新模拟目标阈值；下一次心率样本会按新值生成。 */
    fun setThresholds(onThreshold: Int, offThreshold: Int) {
        this.onThreshold = onThreshold
        this.offThreshold = offThreshold
    }

    fun start(listener: Listener) {
        if (running) return
        running = true
        tick = 0
        handler.post(object : Runnable {
            override fun run() {
                if (!running) return
                val hr = simulateValue(tick)
                listener.onSimulatedHeartRate(hr)
                tick++
                handler.postDelayed(this, 1000L) // 每秒一个值，模拟 BLE 心率通知频率
            }
        })
    }

    fun stop() {
        running = false
        handler.removeCallbacksAndMessages(null)
    }

    /** 是否正在跑。供 Activity 重建后回填 UI 状态（编排者是单例，模拟源可能还在跑）。 */
    fun isRunning(): Boolean = running

    /**
     * 40 秒一轮：低谷 8 秒 → 上升 → 高位 13 秒 → 回落 → 低谷。
     * 峰值保证高于开拍阈值，低谷保证低于停止阈值。
     */
    private fun simulateValue(t: Int): Int {
        val phase = t % 40
        val low = (offThreshold - 10).coerceAtLeast(20)
        val high = (onThreshold + 10).coerceAtMost(240)
        val mid = (low + high) / 2
        return when {
            phase <= 7 -> low + (3 * sin(phase * 0.7)).toInt()
            phase == 8 -> mid
            phase == 9 -> onThreshold
            phase in 10..22 -> high + ((phase % 3) - 1) * 2
            phase == 23 -> mid
            phase == 24 -> offThreshold - 1
            else -> low + (3 * sin((phase - 24) * 0.6)).toInt()
        }.coerceIn(20, 250)
    }
}
