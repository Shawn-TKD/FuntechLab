package com.insta360.heartbeat.ble

import android.bluetooth.BluetoothAdapter
import android.bluetooth.BluetoothDevice
import android.bluetooth.BluetoothManager
import android.bluetooth.le.BluetoothLeScanner
import android.bluetooth.le.ScanCallback
import android.bluetooth.le.ScanFilter
import android.bluetooth.le.ScanResult
import android.bluetooth.le.ScanSettings
import android.content.Context

/**
 * BLE 扫描：只找出广播「标准心率服务 0x180D」的设备（Garmin FR255、Polar 胸带等）。
 *
 * 用途说明：
 *  - 主要对接 Garmin FR255：在手表「心率广播」开启后，手机扫描即可发现。
 *  - 未来 RDK X5 若写一个模拟 0x180D 广播源，也会被这里扫到，两源共用本页。
 */
class HeartRateScanner(private val context: Context) {

    interface Callback {
        /**
         * 发现一台设备。
         *
         * @param rssi 信号强度（负值，越接近 0 越强）。用于把"离你最近的那台"排前面。
         * @param advertisesHeartRate 该设备**广播包**里是否带了标准心率服务 0x180D。
         *   这是比"名字含 Garmin"强得多的判据——广播里带 0x180D 基本可以断定就是心率设备。
         *   注意：为 false 不代表不是心率设备（部分设备不开广播，只在 GATT 里暴露），
         *   所以这里只当**提示**，不做过滤。
         * @param advertisedName 广播包里自带的名字（可能为 null）。
         *
         *   ⚠️ **必须用这个，不要在 UI 里调 `BluetoothDevice.getName()`**。
         *   `getName()` 是一次 binder 调用（要问系统蓝牙服务），而本回调在
         *   SCAN_MODE_LOW_LATENCY 下每秒会被触发几百次；若在渲染路径里对每台设备都调一次
         *   getName()，主线程会被拖死 → MIUI 直接弹「心跳控制相机无响应」ANR。
         *   广播包里的名字是随扫描结果一起送来的，零成本。
         */
        fun onDeviceFound(
            device: BluetoothDevice,
            rssi: Int,
            advertisesHeartRate: Boolean,
            advertisedName: String?,
        )

        fun onScanFinished()
        fun onScanError(msg: String)
    }

    /**
     * ⚠️ 必须惰性求值（by lazy），不能在构造期就取 adapter / bluetoothLeScanner。
     *
     * Android 12(API 31)+ 上 `BluetoothAdapter.getBluetoothLeScanner()` 需要 BLUETOOTH_SCAN 权限，
     * `BluetoothManager.getAdapter()` 需要 BLUETOOTH_CONNECT 权限；而本类在 Activity onCreate 里
     * 就被构造（那时权限还没申请），提前求值会直接抛 SecurityException 导致闪退。
     */
    private val bluetoothManager: BluetoothManager by lazy {
        context.getSystemService(Context.BLUETOOTH_SERVICE) as BluetoothManager
    }

    private val scanner: BluetoothLeScanner?
        get() = runCatching { bluetoothManager.adapter?.bluetoothLeScanner }.getOrNull()

    private val callback = object : ScanCallback() {
        override fun onScanResult(callbackType: Int, result: ScanResult) {
            // 不做 serviceUuid 过滤（过滤会把只在 GATT 里暴露服务的设备漏掉），
            // 但把广播里的服务列表读出来，供 UI 标记「确定是心率设备」。
            // 实测：Garmin 在「心率广播」开启时，广播包里通常会带 0x180D。
            //
            // ⚠️ 这里所有取值都从 result 自带的广播数据里拿（零 binder 调用）。
            // 本回调在 LOW_LATENCY 模式下每秒会触发几百次，任何一次同步 binder 调用都会
            // 累加成主线程卡顿甚至 ANR。
            val record = result.scanRecord
            val advertises = runCatching {
                record?.serviceUuids?.any { it.uuid == HEART_RATE_SERVICE_UUID } ?: false
            }.getOrDefault(false)
            val advertisedName = runCatching { record?.deviceName }.getOrNull()
            listener?.onDeviceFound(result.device, result.rssi, advertises, advertisedName)
        }

        override fun onScanFailed(errorCode: Int) {
            listener?.onScanError("BLE 扫描失败 code=$errorCode（SCAN_FAILED_APPLICATION_REGISTRATION_FAILED=1 时重启蓝牙即可）")
        }
    }

    private var listener: Callback? = null

    /** 到点自动停止扫描的定时任务。手动 [stop] 时要能取消掉，否则它还会再回调一次 onScanFinished。 */
    private var stopTask: Runnable? = null

    private val handler = android.os.Handler(context.mainLooper)

    /** 开始扫描。durationMs 内持续回报所有发现的设备；时间到自动停止。 */
    fun start(
        durationMs: Long = 10000L,
        listener: Callback,
    ) {
        this.listener = listener
        val adapter = runCatching { bluetoothManager.adapter }.getOrNull()
        if (adapter == null) {
            listener.onScanError("设备不支持蓝牙")
            return
        }
        if (runCatching { adapter.isEnabled }.getOrDefault(false).not()) {
            listener.onScanError("蓝牙未开启")
            return
        }
        val s = scanner ?: run {
            listener.onScanError("无法获取 BLE 扫描器（请确认已授予「附近的设备」权限）")
            return
        }
        val settings = ScanSettings.Builder()
            .setScanMode(ScanSettings.SCAN_MODE_LOW_LATENCY)
            .build()
        try {
            // 注意：API 31+ 起 startScan(ScanSettings, ScanCallback) 这个两参重载已被移除，
            // 只剩 startScan(ScanCallback) 与 startScan(List<ScanFilter>, ScanSettings, ScanCallback)。
            // 这里传空的 filters 列表以兼容所有版本。
            s.startScan(emptyList<ScanFilter>(), settings, callback)
            val task = Runnable {
                stopTask = null
                // 只停硬件扫描，不动 listener —— 这样下面这行才能把「扫描自然结束」通知出去。
                stopScanHardware()
                listener.onScanFinished()
            }
            stopTask = task
            handler.postDelayed(task, durationMs)
        } catch (e: Exception) {
            listener.onScanError("启动扫描失败: ${e.message}")
        }
    }

    /**
     * 手动停止扫描。
     *
     * ⚠️ **不会**回调 `onScanFinished()` —— 这是「主动中止」而非「扫描自然结束」。
     * 用户点选设备后就会走到这里：此时不该再弹「扫描结束，共 N 台设备」那种提示，
     * 也不该再触发一次列表重绘（列表马上要被隐藏了）。
     */
    fun stop() {
        stopTask?.let { handler.removeCallbacks(it) }
        stopTask = null
        stopScanHardware()
        listener = null
    }

    private fun stopScanHardware() {
        runCatching { scanner?.stopScan(callback) }
    }

    companion object {
        /** 标准 BLE 心率服务。 */
        val HEART_RATE_SERVICE_UUID: java.util.UUID =
            java.util.UUID.fromString("0000180d-0000-1000-8000-00805f9b34fb")

        /** 标准 BLE 心率测量特征（读取当前心率、支持通知）。 */
        val HEART_RATE_MEASUREMENT_CHARACTERISTIC: java.util.UUID =
            java.util.UUID.fromString("00002a37-0000-1000-8000-00805f9b34fb")
    }
}