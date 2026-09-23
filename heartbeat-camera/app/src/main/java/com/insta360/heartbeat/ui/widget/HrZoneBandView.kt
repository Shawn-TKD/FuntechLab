package com.insta360.heartbeat.ui.widget

import android.content.Context
import android.graphics.Canvas
import android.graphics.Paint
import android.graphics.RectF
import android.util.AttributeSet
import android.view.View
import androidx.core.content.ContextCompat
import com.insta360.heartbeat.R

/**
 * 触发区间带：把「迟滞阈值」这件**要记住的规则**画成**看得见的三个区段**。
 *
 * ```
 * 30 ──────────── off ────── on ──────────────── 250
 * │  安全区（绿）  │ 迟滞带（橙）│   触发区（红）   │
 *                    ▲ 当前心率游标
 * ```
 *
 * ## 为什么必须做这个控件
 *
 * V14 里迟滞机制只存在于 200 字的说明文字里（「心率 ≥ 110 开始，< 100 停止」），
 * 用户得先读懂再记住，然后在心里对照当前心率。
 * 画成区段后，**当前心率落在哪个区、离触发线还有多远**变成一眼可见的事实，
 * 不再依赖记忆 —— 这也是整个 V15 重构里收益最高的一个改动。
 *
 * ## 量程
 *
 * 用 30~250（与设置里的可填范围一致），而不是环上的 40~200：
 * 区间带的作用是「看清三个区的相对位置」，需要完整量程；
 * 环的作用是「感受变化」，需要放大日常区间。两者目的不同，映射也不同，这是刻意的。
 */
class HrZoneBandView @JvmOverloads constructor(
    context: Context,
    attrs: AttributeSet? = null,
    defStyleAttr: Int = 0,
) : View(context, attrs, defStyleAttr) {

    var minBpm: Int = 30
    var maxBpm: Int = 250

    /** 开拍阈值：从右往左第一个分界（触发区起点）。 */
    private var onThreshold: Int = 110

    /** 停止阈值：安全区与迟滞带的分界。 */
    private var offThreshold: Int = 100

    /** 当前心率；<= 0 表示没有数据，不画游标。 */
    private var heartRate: Int = 0

    private val density = resources.displayMetrics.density

    private val fillPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply { style = Paint.Style.FILL }
    private val dividerPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        style = Paint.Style.STROKE
        strokeWidth = 2f * density
    }
    private val cursorPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        style = Paint.Style.FILL
        color = ContextCompat.getColor(context, R.color.text_primary)
    }

    private val rect = RectF()
    private val cursorRect = RectF()

    private val colorOk = ContextCompat.getColor(context, R.color.ok)
    private val colorWarn = ContextCompat.getColor(context, R.color.warn)
    private val colorDanger = ContextCompat.getColor(context, R.color.danger)

    /** 阈值或量程变化时重建描述。 */
    fun setThresholds(on: Int, off: Int) {
        onThreshold = on
        offThreshold = off
        updateContentDescription()
        invalidate()
    }

    fun setHeartRate(hr: Int) {
        if (hr == heartRate) return
        heartRate = hr
        updateContentDescription()
        invalidate()
    }

    fun clearHeartRate() {
        heartRate = 0
        updateContentDescription()
        invalidate()
    }

    private fun xOf(bpm: Int): Float {
        val span = (maxBpm - minBpm).toFloat().coerceAtLeast(1f)
        val f = ((bpm - minBpm) / span).coerceIn(0f, 1f)
        return paddingLeft + f * (width - paddingLeft - paddingRight)
    }

    override fun onDraw(canvas: Canvas) {
        super.onDraw(canvas)
        if (width <= 0 || height <= 0) return

        val left = paddingLeft.toFloat()
        val right = (width - paddingRight).toFloat()
        val top = paddingTop.toFloat()
        val bottom = (height - paddingBottom).toFloat()
        val radius = (bottom - top) / 2f

        rect.set(left, top, right, bottom)

        // 三段底色。alpha 按设计规范：安全 50% / 迟滞 90% / 触发 34%。
        // 迟滞带最亮是刻意的 —— 它最窄，却最需要被看见（「为什么停了却不马上重开」的答案就在这里）。
        val xOff = xOf(offThreshold)
        val xOn = xOf(onThreshold)

        drawSegment(canvas, left, xOff, top, bottom, radius, colorOk, 0.50f)
        drawSegment(canvas, xOff, xOn, top, bottom, radius, colorWarn, 0.90f)
        drawSegment(canvas, xOn, right, top, bottom, radius, colorDanger, 0.34f)

        // 分界线：与环上的刻度同色同义（停止=绿，开拍=红）
        dividerPaint.color = colorOk
        canvas.drawLine(xOff, top, xOff, bottom, dividerPaint)
        dividerPaint.color = colorDanger
        canvas.drawLine(xOn, top, xOn, bottom, dividerPaint)

        // 当前心率游标：2dp 竖线 + 顶端圆点
        if (heartRate > 0) {
            val x = xOf(heartRate)
            cursorPaint.color = ContextCompat.getColor(context, R.color.text_primary)
            canvas.drawRoundRect(
                cursorRect.apply { set(x - 1f * density, top - 2f * density, x + 1f * density, bottom + 2f * density) },
                1f * density,
                1f * density,
                cursorPaint,
            )
            canvas.drawCircle(x, top - 4f * density, 4f * density, cursorPaint)
        }
    }

    private fun drawSegment(
        canvas: Canvas,
        l: Float,
        r: Float,
        top: Float,
        bottom: Float,
        radius: Float,
        color: Int,
        alpha: Float,
    ) {
        if (r - l <= 0f) return
        fillPaint.color = withAlpha(color, alpha)
        // 每一小段自身是圆角矩形，整体叠在一起视觉上就是一条胶囊
        canvas.drawRoundRect(RectF(l, top, r, bottom), radius, radius, fillPaint)
    }

    private fun withAlpha(color: Int, alpha: Float): Int {
        val a = (alpha.coerceIn(0f, 1f) * 255).toInt()
        return (color and 0x00FFFFFF) or (a shl 24)
    }

    private fun updateContentDescription() {
        contentDescription = buildString {
            append("触发区间带：")
            append("低于 $offThreshold 为安全区；")
            append("$offThreshold 到 $onThreshold 为迟滞带（此区间内不会重新触发）；")
            append("达到 $onThreshold 触发拍摄。")
            if (heartRate > 0) append("当前心率 $heartRate。")
        }
    }
}
