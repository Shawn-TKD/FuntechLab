package com.insta360.heartbeat.threshold

/**
 * 心率阈值判定引擎（迟滞 / Hysteresis），防止心率在阈值附近抖动导致相机疯狂开关。
 *
 * 规则：
 *  - 心率先 > = [onThreshold] 且当前为 [State.PAUSED] → 返回 [Action.START]，状态切到 [State.REC]。
 *  - 心率先 <   [offThreshold] 且当前为 [State.REC]    → 返回 [Action.STOP]，状态切到 [State.PAUSED]。
 *  - 之间保持当前状态，返回 [Action.NONE]。
 *
 * on=110 / off=100 的 10 拍回差能有效过滤临界抖动。
 *
 * ⚠️ 阈值现在可在 App 上手动改（见 AppSettings），因此设计成**可变属性**，
 * 并保证任何时刻都满足 `offThreshold < onThreshold`：
 * 直接给 offThreshold 赋一个 >= onThreshold 的值会被自动压回 onThreshold - 1，
 * 否则迟滞失效、会退化成阈值附近疯狂开关相机。
 */
class ThresholdEngine(
    /** 开录阈值（> = 此值开录，默认 110）。 */
    onThreshold: Int = 110,
    /** 暂停阈值（< 此值暂停，默认 100）。恒 < onThreshold。 */
    offThreshold: Int = 100,
) {
    /** 开录阈值。改小到不超过当前暂停阈值时，暂停阈值会被一起下压。 */
    var onThreshold: Int = onThreshold
        set(value) {
            field = value
            if (offThreshold >= value) offThreshold = value - 1
        }

    /** 暂停阈值。赋 >= onThreshold 的值会被压回 onThreshold - 1。 */
    var offThreshold: Int = offThreshold
        set(value) {
            field = if (value >= onThreshold) onThreshold - 1 else value
        }

    init {
        // 构造期也要保证不变量成立
        require(onThreshold > offThreshold) {
            "onThreshold($onThreshold) 必须大于 offThreshold($offThreshold)"
        }
    }

    /** 判定结果：需要操作相机开 / 停 / 不变。 */
    enum class Action {
        START,
        STOP,
        NONE,
    }

    /** 当前录制状态。 */
    enum class State {
        PAUSED,
        REC,
    }

    var state: State = State.PAUSED
        private set

    /** 每次心率变化时调用，返回是否需要触发相机操作。 */
    fun onHeartRate(hr: Int): Action {
        return when (state) {
            State.PAUSED ->
                if (hr >= onThreshold) {
                    state = State.REC
                    Action.START
                } else {
                    Action.NONE
                }

            State.REC ->
                if (hr < offThreshold) {
                    state = State.PAUSED
                    Action.STOP
                } else {
                    Action.NONE
                }
        }
    }

    /** 复位为暂停态（改阈值/切模式后调用，避免状态与相机实际状态不一致）。 */
    fun reset() {
        state = State.PAUSED
    }
}
