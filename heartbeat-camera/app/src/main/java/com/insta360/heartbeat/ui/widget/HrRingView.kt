package com.insta360.heartbeat.ui.widget

import android.animation.ValueAnimator
import android.content.Context
import android.graphics.Canvas
import android.graphics.Paint
import android.graphics.RectF
import android.provider.Settings
import android.util.AttributeSet
import android.view.View
import android.view.animation.DecelerateInterpolator
import androidx.core.content.ContextCompat
import com.insta360.heartbeat.R
import kotlin.math.max
import kotlin.math.min

/**
 * 心率环：把「当前心率」画成一个 240° 的弧形进度环。
 *
 * ## 为什么环的映射区间是 40~200，而阈值范围是 30~250
 *
 * 30~250 这个全量程里的日常变化只占很小一段：心率从 70 变到 90，
 * 在全量程上只有 9% 的弧长变化，肉眼几乎看不出来。
 * 40~200 覆盖了真实的静息~运动区间，同样的变化能占 27% 弧长，**变化看得见**。
 * 超出量程的值会被夹到两端（不是没显示，是顶到头）。
 *
 * ## 起角与缺口
 *
 * 起角 150°、扫角 240°，缺口留在**正下方** —— 一来视觉重心偏上、
 * 读数时不会被手掌挡住；二来缺口正对下方的区间带，视线过渡自然。
 */
class HrRingView @JvmOverloads constructor(
    context: Context,
    attrs: AttributeSet? = null,
    defStyleAttr: Int = 0,
) : View(context, attrs, defStyleAttr) {

    /** 环形映射的低端 bpm。 */
    var minBpm: Int = 40

    /** 环形映射的高端 bpm。 */
    var maxBpm: Int = 200

    /** 当前心率（bpm）。0 或负数表示还没有数据，环只画轨道。 */
    private var heartRate: Int = 0

    /** 已平滑到的比例（0~1），动画就是把它推向目标值。 */
    private var animFraction: Float = 0f

    /** 开拍阈值（环上的刻度）。 */
    private var onThreshold: Int = 110

    /** 停止阈值（环上的刻度）。 */
    private var offThreshold: Int = 100

    private var animator: ValueAnimator? = null

    private val density = resources.displayMetrics.density

    private val trackPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        style = Paint.Style.STROKE
        strokeCap = Paint.Cap.ROUND
        color = ContextCompat.getColor(context, R.color.bg_surface_3)
    }

    private val progressPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        style = Paint.Style.STROKE
        strokeCap = Paint.Cap.ROUND
        color = ContextCompat.getColor(context, R.color.hr)
    }

    /** 阈值刻度：细横线，让「现在离触发线还有多远」一眼可见。 */
    private val tickPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        style = Paint.Style.STROKE
        strokeCap = Paint.Cap.ROUND
    }

    private val arcRect = RectF()

    /** 环宽 13dp（设计规范）。 */
    private var strokePx: Float = 13f * density

    init {
        // 让 View 在 wrap_content 下也有合理默认（布局里给的是 212dp 固定值）
        strokePaint()
    }

    private fun strokePaint() {
        trackPaint.strokeWidth = strokePx
        progressPaint.strokeWidth = strokePx
        tickPaint.strokeWidth = 2f * density
    }

    /** 设置在环上显示的心率；会平滑过渡，不跳变。 */
    fun setHeartRate(hr: Int) {
        if (hr == heartRate) return
        heartRate = hr
        val target = fractionOf(hr)
        animateTo(target)
        updateContentDescription()
    }

    /** 没有数据时调用，环回到空态。 */
    fun clearHeartRate() {
        heartRate = 0
        animateTo(0f)
        updateContentDescription()
    }

    /** 更新阈值刻度（开拍 / 停止）。改设置后调用。 */
    fun setThresholds(on: Int, off: Int) {
        if (on == onThreshold && off == offThreshold) return
        onThreshold = on
        offThreshold = off
        invalidate()
    }

    private fun fractionOf(hr: Int): Float {
        if (hr <= 0) return 0f
        val span = (maxBpm - minBpm).toFloat().coerceAtLeast(1f)
        return ((hr - minBpm) / span).coerceIn(0f, 1f)
    }

    /**
     * 平滑到目标比例。
     *
     * ⚠️ 尊重系统「移除动画」开关：`ANIMATOR_DURATION_SCALE == 0` 时直接跳到终值，
     * 否则套用 250ms 线性跟随（数据驱动，不用缓动，避免看起来「滞后」）。
     */
    private fun animateTo(target: Float) {
        animator?.cancel()
        if (!animationsEnabled()) {
            animFraction = target
            invalidate()
            return
        }
        animator = ValueAnimator.ofFloat(animFraction, target).apply {
            duration = 250L
            interpolator = DecelerateInterpolator()
            addUpdateListener {
                animFraction = it.animatedValue as Float
                invalidate()
            }
            start()
        }
    }

    private fun animationsEnabled(): Boolean = runCatching {
        Settings.Global.getFloat(
            context.contentResolver,
            Settings.Global.ANIMATOR_DURATION_SCALE,
            1f,
        ) != 0f
    }.getOrDefault(true)

    override fun onSizeChanged(w: Int, h: Int, oldw: Int, oldh: Int) {
        super.onSizeChanged(w, h, oldw, oldh)
        val inset = strokePx / 2f + 2f * density
        arcRect.set(
            inset,
            inset,
            w - inset,
            h - inset,
        )
    }

    /**
     * 环宽按控件短边自适应：设计值 13dp 对应 212dp 直径。
     * 控件被放大/缩小时环宽同比缩放，视觉比例才不会走样。
     */
    private fun ensureStrokeMatchesSize() {
        val short = min(width, height)
        if (short <= 0) return
        val expected = short * (13f / 212f)
        if (kotlin.math.abs(expected - strokePx) > 0.5f) {
            strokePx = max(4f * density, expected)
            strokePaint()
            val inset = strokePx / 2f + 2f * density
            arcRect.set(inset, inset, width - inset, height - inset)
        }
    }

    override fun onDraw(canvas: Canvas) {
        super.onDraw(canvas)
        ensureStrokeMatchesSize()
        if (arcRect.isEmpty) return

        // ① 轨道
        canvas.drawArc(arcRect, START_ANGLE, SWEEP_ANGLE, false, trackPaint)

        // ② 阈值刻度（停止 = 绿，开拍 = 心率红，与区间带同一套语义色）
        drawThresholdTick(canvas, offThreshold, R.color.ok)
        drawThresholdTick(canvas, onThreshold, R.color.hr)

        // ③ 进度
        if (animFraction > 0f) {
            canvas.drawArc(arcRect, START_ANGLE, SWEEP_ANGLE * animFraction, false, progressPaint)
        }
    }

    private fun drawThresholdTick(canvas: Canvas, bpm: Int, colorRes: Int) {
        val f = fractionOf(bpm)
        if (f <= 0f || f >= 1f) return
        val angle = Math.toRadians((START_ANGLE + SWEEP_ANGLE * f).toDouble())
        val cx = arcRect.centerX()
        val cy = arcRect.centerY()
        val rOuter = arcRect.width() / 2f
        val rInner = rOuter - strokePx
        tickPaint.color = ContextCompat.getColor(context, colorRes)
        canvas.drawLine(
            cx + (rInner * Math.cos(angle)).toFloat(),
            cy + (rInner * Math.sin(angle)).toFloat(),
            cx + (rOuter * Math.cos(angle)).toFloat(),
            cy + (rOuter * Math.sin(angle)).toFloat(),
            tickPaint,
        )
    }

    private fun updateContentDescription() {
        contentDescription = if (heartRate > 0) {
            "当前心率 $heartRate bpm，开拍阈值 $onThreshold，停止阈值 $offThreshold"
        } else {
            "暂无心率数据，开拍阈值 $onThreshold，停止阈值 $offThreshold"
        }
    }

    override fun onDetachedFromWindow() {
        super.onDetachedFromWindow()
        // 必须取消：环是常驻控件，Activity 重建时若动画还在跑会泄漏（持有 View）
        animator?.cancel()
        animator = null
    }

    private companion object {
        /** 起角：正下方偏左 30°（0° = 3 点钟方向，顺时针为正）。 */
        const val START_ANGLE = 150f

        /** 扫角 240°，缺口 120° 留在正下方。 */
        const val SWEEP_ANGLE = 240f
    }
}
