# heartbeat-camera · BLE 心率连不上的排查记录

> 2026-09-22。现象：**「模拟心率」一切正常，但接真实 Garmin FR255 后心率永远是 `-- bpm`，
> 界面上「心率源」一直停在「连接中…」。**
> 本文记录根因（代码 bug，不是手表设置问题）、修复，以及连不上时的排查顺序。

---

## 一、先说结论

**根因是代码 bug：`HeartRateMonitor.connect()` 里的一句 `close()` 把监听器清成了 null，
导致连接成功、服务发现、心率数据、错误信息全部被静默丢弃。**

所以之前所有「手表设置不对 / 要开心率广播 / 要先配对」的猜测都**不是主因**——
广播开着也没用，因为 App 侧根本没在听。

**这也解释了为什么只有「模拟心率」能跑通**：模拟心率不走 BLE，
是 `SimulatedHeartRateSource` 直接调 `feed(hr)` 喂给阈值引擎，完全绕过了这段坏掉的代码。

---

## 二、根因详解

### 错误代码（v4 及以前）

```kotlin
fun connect(device: BluetoothDevice, listener: Listener) {
    this.listener = listener                      // ① 先赋值
    close()                                       // ② 💥 close() 内部执行了 listener = null
    gatt = device.connectGatt(context, false, gattCallback)
}

fun close() {
    runCatching { gatt?.close() }
    gatt = null
    listener = null                               // ③ 监听器没了
}
```

而 `gattCallback` 里**每一个回调**都是安全调用（`listener?.onXxx()`）：

```kotlin
BluetoothProfile.STATE_CONNECTED -> post { listener?.onConnected(gatt.device) }   // listener == null → 丢弃
```

### 后果链

| 步骤 | 本该发生 | 实际发生 |
|---|---|---|
| GATT 连接成功 | `onConnected` → UI 显示「已连接 FR255」 | 静默丢弃，UI 停在「连接中…」 |
| 服务发现完成 | 找到/找不到 0x180D 都会提示 | 静默丢弃，**用户看不到任何失败原因** |
| 订阅心率 CCCD | 开始推数据 | 静默丢弃 |
| 收到心率 | 顶部数字跳动 | 静默丢弃，永远 `-- bpm` |

### 同源问题

`onConnectionStateChange` 的**断开分支**也是同样的坑：

```kotlin
BluetoothProfile.STATE_DISCONNECTED -> {
    val dev = gatt.device
    close()                                       // 💥 listener 先变 null
    post { listener?.onDisconnected(dev) }        // 于是这句永远不生效
}
```

断线也永远不上报，用户只会看到界面卡住。

---

## 三、修复方式

核心思路：**把「释放 GATT 资源」和「清空监听器」拆成两个方法，各管各的。**

```kotlin
/** 只释放 GATT，保留 listener —— 重连/换设备时用这个 */
private fun releaseGatt() {
    cancelTimeout()
    runCatching { gatt?.close() }
    gatt = null
}

/** 彻底关闭：GATT + listener 一起清 —— 确定不再用了才调 */
fun close() {
    releaseGatt()
    listener = null
    target = null
}
```

`connect()` 里的顺序也改对了（先释放旧 GATT，再设置新 listener，两者不再互相踩）：

```kotlin
fun connect(device: BluetoothDevice, listener: Listener) {
    releaseGatt()              // ① 只清 GATT，不碰 listener
    this.listener = listener   // ② 再设置新 listener
    this.target = device
    openGatt(device)
}
```

断开分支改为**先回调、再释放**：

```kotlin
BluetoothProfile.STATE_DISCONNECTED -> {
    cancelTimeout()
    post { listener?.onDisconnected(dev) }   // ① 先把事件发出去
    releaseGatt()                            // ② 再释放资源
}
```

---

## 四、顺带修掉的三个真实缺陷

### 4.1 `parseHeartRate` 数组越界（会崩溃）

```kotlin
// 旧版：只判 size < 2
if (value == null || value.size < 2) return 0
val flags = value[0].toInt() and 0xFF
return if (flags and 0x01 != 0) {
    (value[1] ...) or ((value[2] ...) shl 8)   // 💥 16bit 但只有 2 字节 → 越界崩溃
}
```

若设备发来 `flags=0x01`（声明 16bit）但只带 2 字节，`value[2]` 直接 `ArrayIndexOutOfBoundsException`。
**改为按位宽分别校验长度。**

### 4.2 未指定 `TRANSPORT_LE`

```kotlin
// 旧版：用双模默认传输
device.connectGatt(context, false, gattCallback)

// 新版：明确指定低功耗传输
device.connectGatt(context, false, gattCallback, BluetoothDevice.TRANSPORT_LE)
```

心率设备是纯 BLE。用默认值在部分手机上会尝试 BR/EDR 侧握手，
表现为「连上了但服务列表是空的」。

### 4.3 GATT 133 无重连

`status=133`（`GATT_ERROR`）在 Android 上是**最常见的**蓝牙错误之一：
配对态脏、连接间隔冲突、刚断开立刻重连都会触发。
旧版只报个错就结束。现在**自动重连最多 3 次**（每次关掉旧 GATT 句柄再连——
直接复用同一个句柄通常会一直失败），仍失败才给出可操作的建议。

---

## 五、UI 侧的配合改动

### 5.1 区分「连上」与「订阅成功」

`Listener` 新增 `onReady()`，在 **CCCD 写入成功**时触发：

```kotlin
override fun onDescriptorWrite(gatt, descriptor, status) {
    if (status == GATT_SUCCESS) {
        post { listener?.onReady(dev) }     // 从这一刻起才会有心率数据
    }
}
```

状态文案因此能给出有意义的进度，而不是含糊的「已连接」：

| 阶段 | 文案 |
|---|---|
| GATT 通了 | 已连接 FR255（正在订阅心率…） |
| CCCD 写成功 | 已连接 FR255（心率订阅中） |
| 收到数据 | 顶部心率开始跳动 |

### 5.2 连接超时兜底

15 秒仍未连上就明确报超时，避免用户对着「连接中…」无限等待：

> 连接超时（15s）。请确认 FR255 已开启「心率广播」，且它没有同时连在别的手机上。

### 5.3 服务列表可见（连错设备时能看出端倪）

找不到 0x180D 时，把**实际发现的服务 UUID**列出来：

> 这台设备没有心率服务 0x180D（发现的服务: 1800, 1801, 180a）。
> 八成是连错设备了，换一台重连。

一眼就能判断「我到底连到了什么东西」。

### 5.4 扫描列表按证据强度排序 + 显示 RSSI

BLE 扫描时读取**广播包里的 service UUID**（这是比设备名硬得多的判据）：

```kotlin
val advertises = result.scanRecord?.serviceUuids?.any { it.uuid == HEART_RATE_SERVICE_UUID }
```

> ⚠️ 注意：**读取但不用于过滤**。部分设备不开广播、只在 GATT 里暴露服务，
> 一旦用 0x180D 做 `ScanFilter`，这些设备就彻底扫不到了。

排序优先级：**广播含 0x180D > 名字像心率设备 > 已配对 > 信号强（RSSI）**。
FR255 就戴在手上，通常是信号最强的那台。

列表第 1 行会标 **「★ 最可能」**，广播含 0x180D 的标 **「✔ 广播含心率服务」**。

---

## 六、以后再遇到「连不上 / 心率不动」，按这个顺序查

**先看日志**（App 底部日志区，或 Logcat 过滤 `com.insta360.heartbeat`）：

| 日志内容 | 含义 | 怎么办 |
|---|---|---|
| `连接超时（15s）` | 根本没连上 | 手表开「心率广播」；确认没被别的手机占用 |
| `连接异常(status=133)，正在重试` | GATT 脏状态 | 等它自动重试；连败就在系统蓝牙里「取消配对」后重配 |
| `这台设备没有心率服务 0x180D（发现的服务: ...）` | **连错设备了** | 换列表里带 ✔ 或 ★ 的那行重连 |
| `设备有心率服务但没有心率特征 0x2A37` | 设备实现不规范 | 换设备 |
| `心率特征缺少 CCCD 描述符` | 设备固件不规范 | 换设备 |
| `写入心率订阅(CCCD)失败` | 订阅被拒 | 重连；重启手表 |
| 订阅成功但心率仍不动 | 手表没在测 | **把表戴上手腕**——FR255 没佩戴时不出心率数据 |

**关键排查点（按可能性排序）：**

1. **手表是否真在广播**：手表屏幕上要有心率图标。FR255 路径：
   `设置 → 传感器及配件 → 腕式心率 → 广播心率 → 开启`。
2. **是否被别的设备占用**：BLE 心率广播通常**只能被一台中心设备订阅**。
   如果手表还连着手机上的 Garmin Connect，先断掉。
3. **是否连错了设备**：看日志里的服务列表，或认准列表带 ✔ 的那行。
4. **是否佩戴**：光学心率传感器需要贴着手腕才出数。
5. **配对信息脏**：系统蓝牙里「取消配对」→ 重新配对 → 再回 App 点连。

> **注意**：FR255 广播的是**标准 BLE 心率服务 0x180D / 特征 0x2A37**，
> 本 App 直接就能读，**不需要给手表写任何程序、也不需要 Garmin 的 SDK**。

---

## 七、本次改动文件

| 文件 | 改动 |
|---|---|
| `ble/HeartRateMonitor.kt` | **重写**：拆 `releaseGatt`/`close`；修 listener 被清空的致命 bug；修断开事件丢失；修 `parseHeartRate` 越界；加 `TRANSPORT_LE`；加 133 自动重连；加 15s 超时；加 `onReady`；CCCD 写入适配 API 33+ 新签名 |
| `ble/HeartRateScanner.kt` | 读取广播 service UUID 作提示（不过滤）；新增 `advertisesHeartRate` 回调参数；错误文案补充 errorCode 释义 |
| `ui/HeartbeatOrchestrator.kt` | `startScan` 改用 `ScannedDevice`（带 rssi + advertisesHeartRate）；`attachHeartRateDevice` 接入 `onReady`，状态文案分阶段 |
| `ui/MainActivity.kt` | 扫描列表按证据强度排序、显示 RSSI、标注「✔ 广播含心率服务」/「★ 最可能」；`hrDevices` 改 `LinkedHashMap<String, ScannedRow>` |
