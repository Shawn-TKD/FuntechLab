# 真机调试 adb 工作流（本机实测）

适用设备：小米 MIX Flip（`beryl` / `24094RAD4C`），adb 已授权。
adb 路径：`D:\AndroidSdk\platform-tools\adb.exe`

---

## 一、能力边界（先看这个，能省很多时间）

| 能力 | 可用 | 说明 |
|---|---|---|
| `logcat` 抓日志 | ✅ | 抓应用日志、崩溃日志 |
| `install -r` 装包 | ✅ | 覆盖安装，保留数据 |
| `uiautomator dump` 读界面 | ✅ | 能拿到控件文本 + 坐标 + `resource-id` |
| `dumpsys` 查状态 | ✅ | 包信息、窗口焦点、屏幕尺寸 |
| `run-as` 读应用私有文件 | ✅ | 读 `shared_prefs` / `databases` |
| `dumpsys dropbox --print` 读历史 ANR/crash | ✅ | **事后排查最可靠的入口** |
| **`input tap/swipe/keyevent` 模拟点击** | ❌ | MIUI 报 `SecurityException: Injecting input events requires ... INJECT_EVENTS permission` |

> **结论：这台手机上 UI 操作必须人工做。** 我只能读状态、装包、抓日志。
> 想解锁自动点击：开发者选项里额外打开「**USB 调试（安全设置）**」——
> 注意它与普通「USB 调试」是**两个独立开关**，很多人只开了后者。

⚠️ 踩坑记：`uiautomator dump` 能成功、看起来一切正常，所以很容易**误以为** `input tap` 也生效了。
实际上 tap 会**静默失败**（在另一次调用里才抛 SecurityException），于是「点了没反应」被误判成「应用卡住」。
**任何时候用 tap 驱动界面，都要先做一次校准**（点一个可验证的控件，然后 dump 确认状态真的变了）。

---

## 二、抓应用日志（中文不乱码的正确姿势）

直接在 PowerShell 里 pipe adb 的中文日志会**乱码**（PowerShell 用 GBK 解码 adb 的 UTF-8 输出）。
必须先设输出编码：

```powershell
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$adb = "D:\AndroidSdk\platform-tools\adb.exe"

# 清空（注意：会同时清掉 events 缓冲，am_anr 记录一起没）
& $adb logcat -c

# 抓本应用的界面日志（MainActivity.log() 已镜像到 logcat）
& $adb logcat -d -v time | Select-String "HeartbeatUI|CameraController|HeartbeatOrchestrator|HeartRateMonitor" |
    Out-File "E:\2026智能影像挑战赛\_log.txt" -Encoding UTF8
```

> `cmd /c "... > file"` 会被安全策略拦下，不要用。
> 结果**一定要写进文件再读**——本机 PowerShell 的 stdout 常被吞。

### 本项目可用的 logcat 标签

| 标签 | 来源 |
|---|---|
| `HeartbeatUI` | `MainActivity.log()` —— 所有界面日志 |
| `CameraController` | `CameraController` 的 `Timber` 调用 |
| `HeartbeatOrchestrator` | `HeartbeatOrchestrator.feed()` 的心率/动作流水 |
| `HeartRateMonitor` | GATT 连接、服务发现、订阅、心率解析 |

看心率流水（最快确认链路通没通）：

```powershell
& $adb logcat -d -v time | Select-String "HeartbeatOrchestrator"
# 期望看到 hr=xx state=PAUSED action=START mode=PHOTO cameraConnected=false
```

---

## 三、查 ANR / 崩溃（事后也能查到）

`logcat -c` 会把 events 缓冲一起清掉，`am_anr` 记录就没了。
但系统的 **dropbox 会留存**，这是事后查 ANR 最可靠的入口：

```powershell
# ① 有没有 ANR / 崩溃（events 缓冲，只保留最近一段）
& $adb logcat -b events -d | Select-String "am_anr|am_crash"

# ② dropbox 里留存的历史记录（★ 最可靠）
& $adb shell dumpsys dropbox --print | Out-File "E:\...\_dropbox.txt" -Encoding UTF8
# 然后从文件里找 "data_app_anr" / Subject: Input dispatching timed out
```

`dumpsys dropbox --print` 会把**压缩的 ANR trace 解压后打印**，包含**主线程完整 Java 堆栈**——
直接就能看出主线程卡在哪个方法。本项目就是靠它定位到 `BluetoothDevice.getName()` 的。

```powershell
# ③ /data/anr/ 目录（通常是 root 才能读，但可以看文件是否存在与时间）
& $adb shell ls -l /data/anr/
```

---

## 四、读界面结构（无 tap 时也能知道界面上有什么）

```powershell
& $adb shell uiautomator dump /sdcard/ui.xml
& $adb shell cat /sdcard/ui.xml | Out-File "E:\...\_ui.xml" -Encoding UTF8
```

XML 是一整行，用 Read 会被截断。用正则解析成「文本 / resource-id / 中心坐标」：

```powershell
$xml = Get-Content "E:\...\_ui.xml" -Raw -Encoding UTF8
$rx = [regex]'text="([^"]*)"[^>]*?resource-id="([^"]*)"[^>]*?bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"'
foreach ($m in $rx.Matches($xml)) {
  $cx = [int](([int]$m.Groups[3].Value + [int]$m.Groups[5].Value)/2)
  $cy = [int](([int]$m.Groups[4].Value + [int]$m.Groups[6].Value)/2)
  "$($m.Groups[1].Value) | $($m.Groups[2].Value) | ($cx,$cy)"
}
```

⚠️ **坐标空间随屏幕方向变化**：这台机器会横竖屏切换。
横屏时 `cur=2400x1080`、`action_bar_root` 中心 x≈1148；竖屏时 `cur=1080x2400`、中心 x≈540。
**先 `& $adb shell dumpsys window displays | Select-String "cur="` 确认方向**，再用 dump 出来的坐标。

---

## 五、读应用私有数据（SharedPreferences 等）

```powershell
& $adb shell "run-as com.insta360.heartbeat ls -l shared_prefs"
& $adb shell "run-as com.insta360.heartbeat cat shared_prefs/heartbeat_settings.xml"
```

> 必须把整条 `run-as ...` 作为一个字符串传（让设备端 shell 处理）。
> 若输出为空，先 `ls -l` 确认文件真的存在。

---

## 六、一键体检脚本

```powershell
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$adb = "D:\AndroidSdk\platform-tools\adb.exe"
$out = @()
$out += "== devices =="
$out += (& $adb devices -l 2>&1)
$out += "== screen =="
$out += (& $adb shell dumpsys window displays 2>&1 | Select-String "cur=" | Select-Object -First 1)
$out += "== 应用版本/安装时间 =="
$out += (& $adb shell dumpsys package com.insta360.heartbeat 2>&1 | Select-String "versionName|firstInstallTime|lastUpdateTime")
$out += "== 进程存活 =="
$out += (& $adb shell pidof com.insta360.heartbeat 2>&1)
$out += "== 历史 ANR/crash =="
$out += (& $adb logcat -b events -d 2>&1 | Select-String "am_anr|am_crash" | Select-Object -Last 10)
$out += "== 最近的界面日志 =="
$out += (& $adb logcat -d -v time 2>&1 | Select-String "HeartbeatUI|CameraController" | Select-Object -Last 30)
$out | Set-Content "E:\2026智能影像挑战赛\_health.txt" -Encoding UTF8
"done"
```

---

## 七、构建 → 装机 → 复测 的完整闭环

```powershell
# 1) 构建
$env:JAVA_HOME="E:\2026智能影像挑战赛\_buildenv\jdk\jdk-17.0.2"
$env:GRADLE_USER_HOME="E:\gradle_home"
$env:ANDROID_HOME="D:\AndroidSdk"
$env:ANDROID_SDK_ROOT="D:\AndroidSdk"
Set-Location "E:\2026智能影像挑战赛\heartbeat-camera"
& "E:\buildtools\gradle-8.11.1\bin\gradle.bat" assembleDebug --offline

# 2) 打包 + 装机 + 清日志 + 启动
$adb = "D:\AndroidSdk\platform-tools\adb.exe"
$src = "E:\2026智能影像挑战赛\heartbeat-camera\app\build\outputs\apk\debug\app-debug.apk"
$dst = "E:\2026智能影像挑战赛\apk输出\心跳控制相机-debug-vN-xxx.apk"
[System.IO.File]::Copy($src, $dst, $true)
& $adb install -r $dst
& $adb shell am force-stop com.insta360.heartbeat
& $adb logcat -c
& $adb shell am start -n com.insta360.heartbeat/.ui.MainActivity

# 3) ↓ 这一步必须人工在手机上操作（本机 adb 不能 tap）↓
#    点「扫描心率设备」/「连接 GO Ultra」，然后回来抓日志
```

`--offline` 是必须的：Maven 依赖走内网不通时靠本地缓存构建。
复用 daemon 约 1 分钟；`--no-daemon` 要 13 分钟。只在改依赖或 APK 体积异常时才需要 `clean`。
