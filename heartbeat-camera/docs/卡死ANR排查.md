# 卡死 / ANR 排查（MIUI 弹「心跳控制相机无响应」）

## 现象

扫描心率设备（或正在点选手表连接）时，界面**整个卡住不动**，几秒后系统弹出：

> 心跳控制相机 无响应 —— 关闭应用 / 等待

即 **ANR（Application Not Responding）**：主线程被某个操作长时间占住，超过 5 秒系统就弹这个框。

---

## 根因：扫描回调洪泛 + 排序比较器里调 `BluetoothDevice.getName()`

### 系统 ANR 堆栈（真机铁证，2026-09-22 22:35 / 22:36 / 22:37 连爆三次）

抓取方式：`adb shell dumpsys dropbox --print`

```
Subject: Input dispatching timed out
  (com.insta360.heartbeat/com.insta360.heartbeat.ui.MainActivity (server) is not responding.
   Waited 5001ms for MotionEvent(action=MOVE))

"main" prio=5 tid=1 TimedWaiting
  at jdk.internal.misc.Unsafe.park(Native method)
  - waiting on an unknown object
  at java.util.concurrent.locks.LockSupport.parkNanos(LockSupport.java:252)
  at java.util.concurrent.CompletableFuture$Signaller.block(CompletableFuture.java:1842)
  at java.util.concurrent.ForkJoinPool.managedBlock(ForkJoinPool.java:3437)
  at java.util.concurrent.CompletableFuture.timedGet(CompletableFuture.java:1915)
  at java.util.concurrent.CompletableFuture.get(CompletableFuture.java:2071)
  at com.android.bluetooth.x...SynchronousResultReceiver.awaitResultNoInterrupt(...)   ← 同步等蓝牙服务回包
  at android.bluetooth.BluetoothDevice.getName(BluetoothDevice.java:1653)            ← ★ 主线程卡死在这里
  at com.insta360.heartbeat.ui.MainActivity$sortedRows$$inlined$thenByDescending$1.compare
  at java.util.TimSort.binarySort(TimSort.java:296)
  at java.util.Arrays.sort(Arrays.java:1263)
  at com.insta360.heartbeat.ui.MainActivity.sortedRows(MainActivity.kt:435)
  at com.insta360.heartbeat.ui.MainActivity.renderDeviceList(MainActivity.kt:445)
```

这 3 行就是全部真相：

| 帧 | 含义 |
|---|---|
| `SynchronousResultReceiver.awaitResultNoInterrupt` | `getName()` 不是读内存字段，而是**同步阻塞**地等系统蓝牙服务回包 |
| `...inlined$thenByDescending$1.compare` | 它被放在**排序比较器**里 —— 每次比较都调一次，不是每台设备一次 |
| `renderDeviceList(MainActivity.kt:445)` | 而 `renderDeviceList` 又被**每个扫描回调**调用一次 |

### 三个因素相乘

1. **`SCAN_MODE_LOW_LATENCY`**：每收到一个广播包就回调一次 `onScanResult`（同一台设备反复回调），
   几十台设备在场时可达 **每秒数百次**。且这个回调**在主线程**。
2. **每次回调 → `renderDeviceList()`** → `removeAllViews()` + 每台设备新建 2 个 TextView。
3. **`sortedRows()` 的比较器里调 `device.name`**：
   TimSort 的比较次数是 **O(n log n)**。50 台设备 ≈ 300 次比较 ≈
   **300 次同步阻塞 binder 调用**；再乘上每秒数百次重绘。

单次 `getName()` 在 MIUI 上最坏会阻塞数秒（`awaitResultNoInterrupt` 自带超时兜底），
所以主线程一旦撞上就超过 5 秒 → 系统判 ANR。

**数值上的荒谬程度**：按 50 台设备、每秒 300 次回调估算，主线程每秒要完成
`300 × 50 × 2 = 30,000` 次 View 创建 和 `300 × 300 = 90,000` 次阻塞 binder 调用。

### 为什么用户感觉是「连接手表的时候」卡住

扫描按钮一点就开始 10 秒持续扫描，用户在这 10 秒里正低头在列表里找自己的手表 ——
卡死就发生在这个窗口内，所以主观感受是「连手表时卡住了」。

---

## 修法（v9）

三层防护，缺一不可：

### ① 把 `device.name` 彻底移出渲染与排序路径

```kotlin
// HeartRateScanner：名字从广播包里取，零 binder 调用
val advertisedName = runCatching { record?.deviceName }.getOrNull()
listener?.onDeviceFound(result.device, result.rssi, advertises, advertisedName)
```

```kotlin
// MainActivity：ScannedRow 里缓存 name，比较器只读缓存字段
private data class ScannedRow(val device: BluetoothDevice, val name: String, ...)

private fun sortedRows() = hrDevices.values.sortedWith(
    compareByDescending<ScannedRow> { it.advertisesHr }
        .thenByDescending { looksLikeHeartRate(it.name) }   // ← 不再调 device.name
        ...
)
```

> `ScanResult.scanRecord.deviceName` 是**随扫描结果一起送来的**，读它不产生任何 binder 调用。
> 只有「已配对设备」那一小撮才在扫描开始时读一次 `device.name`（数量少、只读一次，可接受）。

### ② 节流：合并高频回调

```kotlin
private fun scheduleRender() {
    if (renderScheduled) return          // 已排期就不再排
    renderScheduled = true
    renderHandler.postDelayed({
        renderScheduled = false
        renderDeviceListNow()
    }, RENDER_THROTTLE_MS)              // 120ms ≈ 8fps
}
```
无论回调来得多密，**最多每 120ms 重绘一次**。主线程压力直接降到 1/50 以下。

### ③ 签名比对 + 复用行视图

```kotlin
val sig = rows.joinToString("|") {
    "${it.device.address}:${it.advertisesHr}:${it.bonded}:${it.rssi / 10}"
}
if (sig == lastRenderSignature) return   // 内容没变，一个 View 都不碰
```

> ⚠️ 信号强度**必须按 10dBm 分档**（`rssi / 10`）再进签名。
> 如果用完整 RSSI，它每变 1 dBm 签名就变一次，等于没节流。

```kotlin
private fun rowView(row: ScannedRow, isTop: Boolean): LinearLayout {
    val cached = rowViews[address]
    if (cached != null) { bindRow(cached, row, isTop); return cached }  // 只改文字，不新建
    ...
}
```

---

## 怎么确认修好了

```bash
adb logcat -c && adb logcat -s HeartbeatUI CameraController
# 然后在 App 里点「扫描心率设备」，观察 10 秒
```

判断标准：

- 界面**始终可滚动、按钮可点**，不出现系统「无响应」弹框；
- 设备列表能持续刷新（新设备会加进来）；
- logcat 里没有 `ANR in com.insta360.heartbeat`；
- 复查系统记录应为空：
  ```bash
  adb logcat -b events -d | grep am_anr          # 应无新记录
  adb shell dumpsys dropbox --print | grep -c "data_app_anr"
  ```

### ⚠️ 本机限制：adb 只能读日志/装包，**不能模拟点击**

这台机器（小米 MIX Flip，MIUI）上 `adb shell input tap/swipe/keyevent` 会直接报错：

```
java.lang.SecurityException: Injecting input events requires the caller
  (or the source of the instrumentation, if any) to have the INJECT_EVENTS permission.
```

也就是说 **UI 操作必须人工在手机上做**。若希望 adb 能驱动界面，
需在开发者选项里额外打开「**USB 调试（安全设置）**」（与普通「USB 调试」是两个独立开关）。

可用的 adb 能力（实测）：`logcat` 抓日志、`install -r` 装包、`uiautomator dump` 读界面结构、
`dumpsys` 查状态、`run-as` 读应用私有文件。

---

## 顺带修掉的同类隐患

同一个「主线程高频路径」上还发现：

1. **`tvLog.text = logBuffer.toString()`**：日志区每次追加都整串重设。
   当前日志量小，暂不构成问题；**但如果以后加高频日志（比如每拍一条），要一并节流**。

2. **`onServicesDiscovered` 里 `it.uuid.toString().substring(4, 8)`**：
   部分协议栈返回 16 位短 UUID（如 `"180d"`，仅 4 字符），`substring(4,8)` 会抛
   `StringIndexOutOfBoundsException`。而这段跑在 **GATT 回调线程**里，
   异常会**直接崩溃 App**（不是温和的"连接失败"）。已改为长度判断后再截取。

---

## 经验规则（本项目适用）

> **任何被高频回调（BLE 扫描 / GATT 通知 / 传感器）触发的 UI 更新，都必须：**
> 1. 节流（合并到固定帧率）；
> 2. 做内容签名比对，未变化不碰 View；
> 3. 不在回调路径里做 binder 调用（`device.name`、`getBondState()`、`dumpsys` 类操作）；
> 4. 回调里不做全量 `removeAllViews()` + 重建。
