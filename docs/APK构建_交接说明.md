# APK 构建 · 交接说明

> 本文件用于把「生成 APK」这条线交接给新会话。原会话（2026-09-22 上午）已停手，不再改动构建相关文件。

## 一、当前进度：环境已就绪，只差跑一次 Gradle

构建环境已全部配好，**不需要再下载任何东西**。下一步就是执行构建命令。

## 二、环境清单（均已验证存在）

| 组件 | 位置 | 状态 |
|---|---|---|
| JDK 17.0.2 | `E:\2026智能影像挑战赛\_buildenv\jdk\jdk-17.0.2` | ✅ 可用 |
| android-35 平台 | `D:\AndroidSdk\platforms\android-35`（`android.jar` 已就位） | ✅ |
| build-tools 35.0.0 | `D:\AndroidSdk\build-tools\35.0.0`（aapt2 / d8 / zipalign 齐全） | ✅ |
| platform-tools（adb） | `D:\AndroidSdk\platform-tools\adb.exe` | ✅ |
| SDK 授权 | `D:\AndroidSdk\licenses\android-sdk-license` | ✅ |
| Gradle Wrapper | `gradle-8.11.1-bin.zip`，jar + gradlew.bat 均在 | ✅（首次构建需联网下载发行包） |
| 工程 | `E:\2026智能影像挑战赛\heartbeat-camera` | ✅ |

> 备注：build-tools 的 zip 内层目录原名为 `android-15`（API 35 的内部代号），已重命名为 `35.0.0` 并补写了 `source.properties`。

## 三、构建命令

```powershell
$env:JAVA_HOME = "E:\2026智能影像挑战赛\_buildenv\jdk\jdk-17.0.2"
$env:GRADLE_USER_HOME = "E:\2026智能影像挑战赛\_buildenv\gradle_home"   # 缓存放 E 盘，别占 C 盘
cd E:\2026智能影像挑战赛\heartbeat-camera
.\gradlew.bat assembleDebug --no-daemon
```

产物路径：`heartbeat-camera\app\build\outputs\apk\debug\app-debug.apk`

装到手机：

```powershell
D:\AndroidSdk\platform-tools\adb.exe install -r .\app\build\outputs\apk\debug\app-debug.apk
```

## 四、原会话已做的两处代码修复（新会话无需重做）

1. **`gradle\libs.versions.toml`** — 新增 `[versions] timber = "5.0.1"`。
   原先 `timber = { version.ref = "timber" }` 指向了一个不存在的 key，会**直接构建失败**。

2. **`app\src\main\java\com\insta360\heartbeat\camera\NanProcessNetworkBinding.kt`** — 重写。
   原实现里 `block()` 可能被调用两次（注册回调 + 已有默认网络分支），会导致**重复发起相机激活请求**；
   现用 flag 去重，并加 5 秒超时兜底，避免连不上公网时卡死。

## 五、已知坑（踩过的）

- **`tar -xf` 解压 zip 会让 PowerShell 会话中断**（返回 exit -1，日志也写不出）。
  改用 `Expand-Archive`，或直接用 7-Zip。
- **`sdkmanager` 在 JDK 17 上跑不起来**（`javax.xml.bind.annotation.XmlSchema` ClassNotFoundException，老版本 sdkmanager 26.1.1 不兼容 JDK17）。
  这就是当初改为「从 dl.google.com 直接下 zip 手工解压」的原因，现已完成，不必再碰 sdkmanager。
- **APK 仅含 arm64-v8a**（影石 SDK 只提供 arm64 原生库），**x86 模拟器装不上，必须真机**。
- **Insta360 官方 Maven 需要凭据**，已写在 `heartbeat-camera\gradle.properties`（该文件被 .gitignore 忽略）：
  `insta360MavenUser=<你的用户名>` / `insta360MavenPassword=<你的密码>`。断网构建会失败，需联网拉依赖。
- **`local.properties`** 已指向 `D:\AndroidSdk`，勿改。
- C 盘只剩 ~9GB，`GRADLE_USER_HOME` 务必设在 E 盘。

## 六、装到手机后怎么测

工程内置了**模拟心率源**，没有 Garmin 手表也能验证全链路：

1. 打开 App → 授予蓝牙/定位/通知权限
2. 点「启动模拟心率」→ 观察心率数字按曲线冲到 110 以上
3. 心率 ≥110 时界面应显示「相机: 开录 ▲」；回落到 <100 时显示「相机: 暂停 ▼」

真实链路（需要 GO Ultra 真机）：
1. GO Ultra 开机进入 WiFi 热点模式
2. 手机系统 WiFi 连上该热点
3. App 内点「连接 GO Ultra」
4. 填 AppId / SecretKey 点「激活」（激活走公网，手机需另有蜂窝通道）
5. 点「扫描 BLE 心率设备」，Garmin FR255 需先开启「心率广播」
