# 代码包说明

本包是「心跳控制相机」项目的**纯代码打包**（2026-09-23 导出）。

## 包内结构

```
心跳控制相机-源码/
├─ heartbeat-camera/          Android App 工程
│  ├─ app/src/main/java/      Kotlin 源码（BLE / 相机 / 阈值引擎 / 上传 / UI）
│  ├─ app/src/main/res/       布局、drawable、颜色/尺寸/样式 token（含 values-night 深色）
│  ├─ app/build.gradle.kts    模块构建脚本
│  ├─ gradle/libs.versions.toml  版本目录
│  ├─ docs/                   9 篇排查与设计文档
│  ├─ server/upload_server.py 本地联调用的上传接收服务（纯标准库）
│  └─ gradle.properties.example  ⚠️ 凭据模板，见下
├─ deploy/                    阿里云 ECS 部署与探测脚本（sh / py）
└─ 设计/                      V15 UI 设计规范 + 高保真 HTML 原型
```

## ⚠️ 拿到后第一步：补 gradle.properties

原始的 `heartbeat-camera/gradle.properties` **被刻意排除**了 —— 它里面是真实的
Insta360 Maven 凭据（该文件本来就在 `.gitignore` 里，不应随代码分发）。

不补会出现依赖解析失败。做法：

```bash
cd heartbeat-camera
cp gradle.properties.example gradle.properties
# 然后填入 Insta360 Maven 的用户名与密码
```

## 刻意的排除项（都属"非代码"）

| 类别 | 具体内容 |
|---|---|
| 构建产物 | `app/build/`、`.gradle/`、`.kotlin/` |
| 运行时环境 | `_buildenv/`（JDK + Gradle，约 640 MB） |
| SDK 与二进制 | `Android-SDK-2.1.5/` 及其 zip（约 470 MB） |
| APK 归档 | `apk输出/`、`阶段的apk/` |
| 机器相关配置 | `local.properties`（写死了本机 SDK 路径） |
| 凭据 | `gradle.properties`（已改为提供 `.example` 模板） |
| 临时与日志 | `_diag/`、`_tmp_sdk/`、`_prefs_backup/`、根目录各类 `.txt` / `.log` / `.png` |
| 编译缓存 | `server/__pycache__/`、`*.pyc` |
| 会话数据 | `.workbuddy/`（项目记忆与工作日志，非代码） |

## 构建方式

```bash
cd heartbeat-camera
./gradlew assembleDebug
```

工程要求：JDK 17、Android SDK（compileSdk 35 / targetSdk 35 / minSdk 29）、
产物 ABI 为 `arm64-v8a`。只依赖 `sdk-camera`（刻意不引 `sdk-media`，
后者会带来约 155 MB 原生库）。详见 `heartbeat-camera/docs/构建与踩坑.md`。
