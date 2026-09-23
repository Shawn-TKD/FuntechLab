package com.insta360.heartbeat.ble

import android.annotation.SuppressLint
import android.bluetooth.BluetoothDevice
import android.bluetooth.BluetoothGatt
import android.bluetooth.BluetoothGattCallback
import android.bluetooth.BluetoothGattCharacteristic
import android.bluetooth.BluetoothGattDescriptor
import android.bluetooth.BluetoothProfile
import android.content.Context
import android.os.Build
import android.os.Handler
import android.os.Looper
import timber.log.Timber

/**
 * 订阅单个心率设备的 0x2A37 特征，解析并回调实时心率。
 *
 * 心率测量值格式（BLE 标准，来自 Bluetooth SIG）：
 *   Byte0: Flags —— bit0: 0=8bit 心率, 1=16bit 心率
 *   Byte1..: 心率值（8bit 或 16bit 小端）
 *   之后若有传感器接触/能耗等附加字段，本最小实现不关心。
 *
 * ============================================================================
 * ⚠️ 历史致命 Bug（v4 及以前，BLE 心率完全不工作，只有模拟心率能跑）
 * ============================================================================
 * 旧版 `connect()` 是这样写的：
 *
 *     fun connect(device, listener) {
 *         this.listener = listener     // ① 先赋值
 *         close()                      // ② close() 内部把 listener 置为 null！
 *         gatt = device.connectGatt(...)
 *     }
 *
 * 而所有回调都是 `listener?.onXxx()`。listener 被 ② 清成 null 后，
 * **连接成功、服务发现、心率数据、错误信息，全部静默丢弃**，
 * UI 就永远停在「心率源: 连接中…」，顶部心率永远 `-- bpm`。
 * 同源问题：`onConnectionStateChange` 的断开分支先 `close()` 再 `post { listener?.onDisconnected }`，
 * 断开事件同样永远不上报。
 *
 * 现在的修法：**把「释放 GATT 资源」和「清空监听器」拆成两个方法**——
 *   - [releaseGatt]：只关 GATT，保留 listener（重连时用）
 *   - [close]：关 GATT + 清 listener（彻底不用了才调）
 * 并且 `connect()` 里先 `releaseGatt()` 再赋值 listener，顺序不会互相踩。
 *
 * 另外顺手修掉的两个真实缺陷：
 *   1. `parseHeartRate` 在「16bit 心率但数据只有 2 字节」时会数组越界崩溃（已加长度判断）。
 *   2. GATT 133 错误（Android 上极常见）原先只报个错就完事，现在会自动重连几次。
 */
class HeartRateMonitor(private val context: Context) {

    interface Listener {
        /** 每次收到一个心率通知。hr 为 int（bpm）。 */
        fun onHeartRate(hr: Int)

        fun onConnected(device: BluetoothDevice)

        fun onDisconnected(device: BluetoothDevice)

        fun onError(msg: String)

        /**
         * 心率订阅已真正开启（CCCD 写入成功）。
         * 与 [onConnected] 分开：连上 ≠ 能收到心率，只有订阅成功才会有数据。
         */
        fun onReady(device: BluetoothDevice) {}
    }

    private var gatt: BluetoothGatt? = null
    private var listener: Listener? = null

    /** 当前连接的设备，重连时要用。 */
    private var target: BluetoothDevice? = null

    /**
     * GATT 是否已连上。
     *
     * ⚠️ 不用 `BluetoothGatt.connectionState()` —— 那个方法 **API 33 才加入**，
     * 在 minSdk 29 的工程里直接编译不过（Unresolved reference）。
     * 自己维护一个标志位，任何版本都能用。
     */
    private var connected = false

    /** 133 重连计数。 */
    private var retry = 0

    /**
     * GATT 是否已连上（自维护标志位）。
     *
     * 供 Activity 重建后回填 UI：编排者是进程级单例，手表连接在重建后依然活着，
     * 新界面必须能问出「现在还连着吗」，否则会错误显示成「未连接」。
     */
    fun isConnected(): Boolean = connected

    /** 当前连接的心率设备名（未连接为 null）。 */
    fun currentDeviceName(): String? =
        target?.let { runCatching { it.name }.getOrNull() ?: it.address }

    /** 连接/服务发现超时兜底，避免 UI 永远卡在「连接中…」。 */
    private val main = Handler(Looper.getMainLooper())
    private var timeoutTask: Runnable? = null

    /** 连接心率设备并开启通知。一次只连一台。 */
    @SuppressLint("MissingPermission")
    fun connect(device: BluetoothDevice, listener: Listener) {
        // ⚠️ 顺序关键：先释放旧的 GATT（不动 listener），再设置新 listener。
        // 反过来写就会被 releaseGatt/close 把刚设好的 listener 清掉。
        releaseGatt()
        this.listener = listener
        this.target = device
        this.retry = 0
        openGatt(device)
    }

    @SuppressLint("MissingPermission")
    private fun openGatt(device: BluetoothDevice) {
        cancelTimeout()
        val name = runCatching { device.name }.getOrNull() ?: device.address
        Timber.d("connectGatt -> %s (retry=%d)", name, retry)

        // 明确指定 TRANSPORT_LE：心率设备是纯 BLE，用双模默认值有时会握手到 BR/EDR 侧导致服务发现为空。
        gatt = runCatching {
            device.connectGatt(
                context,
                false,
                gattCallback,
                BluetoothDevice.TRANSPORT_LE,
            )
        }.getOrElse { e ->
            notifyError("发起连接失败: ${e.message}")
            null
        }

        if (gatt == null) {
            notifyError("connectGatt 返回 null，请确认蓝牙已开启且有「附近的设备」权限")
            return
        }

        // 15 秒还没连上就报超时，免得用户对着「连接中…」干等
        val t = Runnable {
            if (!connected) {
                notifyError("连接超时（15s）。请确认 FR255 已开启「心率广播」，且它没有同时连在别的手机上。")
            }
        }
        timeoutTask = t
        main.postDelayed(t, 15_000L)
    }

    fun disconnect() {
        runCatching { gatt?.disconnect() }
    }

    /**
     * 只释放 GATT 资源，**保留 listener**。
     * 重连、切换设备时必须用这个；用 [close] 会把回调通道一起关掉。
     */
    private fun releaseGatt() {
        cancelTimeout()
        connected = false
        runCatching { gatt?.close() }
        gatt = null
    }

    /** 彻底关闭：释放 GATT + 清空 listener。确定不再接收任何回调时才调。 */
    fun close() {
        releaseGatt()
        listener = null
        target = null
    }

    private fun cancelTimeout() {
        timeoutTask?.let { main.removeCallbacks(it) }
        timeoutTask = null
    }

    private val gattCallback =
        object : BluetoothGattCallback() {

            @SuppressLint("MissingPermission")
            override fun onConnectionStateChange(
                gatt: BluetoothGatt,
                status: Int,
                newState: Int,
            ) {
                val dev = gatt.device
                val name = runCatching { dev.name }.getOrNull() ?: dev.address
                Timber.d("onConnectionStateChange status=%d newState=%d (%s)", status, newState, name)

                if (status != BluetoothGatt.GATT_SUCCESS) {
                    // 133 = GATT_ERROR，Android 上极常见（配对态脏、连接间隔冲突、刚断就连）。
                    // 处理办法：关掉 GATT 句柄后重连，直接重连同一个句柄通常会一直失败。
                    Timber.w("GATT 状态异常 status=%d", status)
                    cancelTimeout()
                    releaseGatt()
                    if (retry < MAX_RETRY) {
                        retry++
                        notifyError("连接异常(status=$status)，正在重试 $retry/$MAX_RETRY …")
                        target?.let { d ->
                            main.postDelayed({ openGatt(d) }, 600L)
                        }
                    } else {
                        notifyError(
                            "连接失败 status=$status（已重试 $MAX_RETRY 次）。" +
                                "常见原因：手表没开「心率广播」/ 已被别的手机占用 / 系统蓝牙里配对信息已脏。" +
                                "可在系统蓝牙里「取消配对」后重新配对再试。"
                        )
                    }
                    return
                }

                when (newState) {
                    BluetoothProfile.STATE_CONNECTED -> {
                        connected = true
                        retry = 0
                        post { listener?.onConnected(dev) }
                        // 连接成功后异步发现服务
                        val started = runCatching { gatt.discoverServices() }.getOrDefault(false)
                        if (!started) {
                            notifyError("启动服务发现失败，请重试连接")
                        }
                    }

                    BluetoothProfile.STATE_DISCONNECTED -> {
                        connected = false
                        cancelTimeout()
                        // ⚠️ 顺序关键：先取出 listener 回调，再释放资源。
                        // 旧版先 close()（listener=null）再回调 → 断开事件永远丢。
                        post { listener?.onDisconnected(dev) }
                        releaseGatt()
                    }
                }
            }

            override fun onServicesDiscovered(gatt: BluetoothGatt, status: Int) {
                if (status != BluetoothGatt.GATT_SUCCESS) {
                    notifyError("服务发现失败 status=$status，请重试连接")
                    return
                }

                val services = runCatching { gatt.services }.getOrNull().orEmpty()
                val hrService = gatt.getService(HeartRateScanner.HEART_RATE_SERVICE_UUID)
                if (hrService == null) {
                    // 把实际发现的服务列出来，便于判断「到底连到了什么设备」。
                    // ⚠️ 不能直接 substring(4,8)：有些协议栈返回的是 16 位短 UUID（如 "180d"，仅 4 字符），
                    // 那样 substring 会抛 StringIndexOutOfBoundsException，而这里在 GATT 回调线程里，
                    // 异常会直接导致整个 App 崩溃（不是「连接失败」那么温和）。
                    val found = services.joinToString(", ") { svc ->
                        val s = svc.uuid.toString()
                        if (s.length >= 8) s.substring(4, 8) else s
                    }
                    notifyError(
                        "这台设备没有心率服务 0x180D（发现的服务: " +
                            (if (found.isBlank()) "无" else found) +
                            "）。八成是连错设备了，换一台重连。"
                    )
                    return
                }

                val hrChar = hrService.getCharacteristic(
                    HeartRateScanner.HEART_RATE_MEASUREMENT_CHARACTERISTIC
                )
                if (hrChar == null) {
                    notifyError("设备有心率服务但没有心率特征 0x2A37")
                    return
                }

                val props = hrChar.properties
                val wantIndicate = (props and BluetoothGattCharacteristic.PROPERTY_NOTIFY) == 0 &&
                    (props and BluetoothGattCharacteristic.PROPERTY_INDICATE) != 0

                // 1) 先让系统把该特征的变更事件转发到本回调
                val localOk = runCatching {
                    gatt.setCharacteristicNotification(hrChar, true)
                }.getOrDefault(false)
                if (!localOk) {
                    notifyError("开启心率通知失败（setCharacteristicNotification 返回 false）")
                    return
                }

                // 2) 再写 CCCD(0x2902)，真正让设备开始推数据
                val cccd = hrChar.getDescriptor(UUID_CCCD_DESCRIPTOR)
                if (cccd == null) {
                    notifyError("心率特征缺少 CCCD 描述符，无法订阅（设备固件实现不规范）")
                    return
                }
                val value =
                    if (wantIndicate) {
                        BluetoothGattDescriptor.ENABLE_INDICATION_VALUE
                    } else {
                        BluetoothGattDescriptor.ENABLE_NOTIFICATION_VALUE
                    }
                val writeOk = writeDescriptorCompat(gatt, cccd, value)
                if (!writeOk) {
                    notifyError("写入心率订阅(CCCD)失败，请重试连接")
                }
            }

            /** CCCD 写入成功 = 订阅真正生效，此后才会有 onCharacteristicChanged。 */
            override fun onDescriptorWrite(
                gatt: BluetoothGatt,
                descriptor: BluetoothGattDescriptor,
                status: Int,
            ) {
                if (descriptor.uuid != UUID_CCCD_DESCRIPTOR) return
                val dev = gatt.device
                if (status == BluetoothGatt.GATT_SUCCESS) {
                    cancelTimeout()
                    post { listener?.onReady(dev) }
                } else {
                    notifyError("心率订阅写入被拒绝 status=$status，请重试连接")
                }
            }

            // ---- 心率数据 ----
            // API 33+ 走带 value 的新重载；老系统走旧重载。两个都实现，保证任何版本都能收到。

            override fun onCharacteristicChanged(
                gatt: BluetoothGatt,
                characteristic: BluetoothGattCharacteristic,
                value: ByteArray,
            ) {
                handleHeartRate(characteristic, value)
            }

            @Deprecated("Deprecated in Java")
            @Suppress("DEPRECATION")
            override fun onCharacteristicChanged(
                gatt: BluetoothGatt,
                characteristic: BluetoothGattCharacteristic,
            ) {
                handleHeartRate(characteristic, characteristic.value)
            }
        }

    private fun handleHeartRate(
        characteristic: BluetoothGattCharacteristic,
        value: ByteArray?,
    ) {
        if (characteristic.uuid != HeartRateScanner.HEART_RATE_MEASUREMENT_CHARACTERISTIC) return
        val hr = parseHeartRate(value)
        if (hr in 20..250) { // 合理范围过滤异常值
            post { listener?.onHeartRate(hr) }
        }
    }

    /** 写 CCCD。API 33+ 用新签名，老版本用废弃的 `descriptor.value = ...` + `writeDescriptor`。 */
    @SuppressLint("MissingPermission")
    private fun writeDescriptorCompat(
        gatt: BluetoothGatt,
        descriptor: BluetoothGattDescriptor,
        value: ByteArray,
    ): Boolean = runCatching {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            gatt.writeDescriptor(descriptor, value) == BluetoothGatt.GATT_SUCCESS
        } else {
            @Suppress("DEPRECATION")
            descriptor.value = value
            @Suppress("DEPRECATION")
            gatt.writeDescriptor(descriptor)
        }
    }.getOrDefault(false)

    /**
     * 解析 0x2A37 心率测量值。
     *
     * ⚠️ 长度必须按位宽分别校验：旧版只判 `size < 2` 就 return，
     * 但 16bit 分支会访问 `value[2]`——若设备发来 flags=0x01 且只带 2 字节，就会数组越界崩溃。
     */
    private fun parseHeartRate(value: ByteArray?): Int {
        if (value == null || value.isEmpty()) return 0
        val flags = value[0].toInt() and 0xFF
        return if (flags and 0x01 != 0) {
            if (value.size < 3) return 0 // 16-bit 需要至少 3 字节
            (value[1].toInt() and 0xFF) or ((value[2].toInt() and 0xFF) shl 8)
        } else {
            if (value.size < 2) return 0 // 8-bit 需要至少 2 字节
            value[1].toInt() and 0xFF
        }
    }

    private fun post(block: () -> Unit) {
        main.post(block)
    }

    private fun notifyError(msg: String) {
        Timber.w("%s", msg)
        post { listener?.onError(msg) }
    }

    companion object {
        val UUID_CCCD_DESCRIPTOR: java.util.UUID =
            java.util.UUID.fromString("00002902-0000-1000-8000-00805f9b34fb")

        /** GATT 异常时的最大重连次数。 */
        private const val MAX_RETRY = 3
    }
}
