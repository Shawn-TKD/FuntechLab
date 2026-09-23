// 根构建脚本：仅声明插件版本，实际应用在 app/build.gradle.kts。
plugins {
    alias(libs.plugins.android.application) apply false
    alias(libs.plugins.kotlin.android) apply false
}
