import org.jetbrains.kotlin.gradle.dsl.JvmTarget

plugins {
    alias(libs.plugins.android.application)
    alias(libs.plugins.kotlin.android)
}

android {
    namespace = "com.insta360.heartbeat"
    compileSdk = 35

    defaultConfig {
        applicationId = "com.insta360.heartbeat"
        minSdk = 29
        targetSdk = 35
        versionCode = 1
        versionName = "1.0.0"

        // 影石 SDK 仅提供 arm64 原生库，x86 模拟器无法运行，必须真机。
        ndk {
            abiFilters += listOf("arm64-v8a")
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            proguardFiles(
                getDefaultProguardFile("proguard-android-optimize.txt"),
                "proguard-rules.pro"
            )
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    kotlin {
        compilerOptions {
            jvmTarget.set(JvmTarget.JVM_17)
        }
    }

    buildFeatures {
        viewBinding = true
        // AGP 8 起 buildConfig 默认关闭；CrashLogger 需要 BuildConfig.APPLICATION_ID / VERSION_NAME
        buildConfig = true
    }

    lint {
        abortOnError = false
    }
}

dependencies {
    implementation(libs.androidx.core.ktx)
    implementation(libs.androidx.appcompat)
    implementation(libs.material)
    implementation(libs.androidx.constraintlayout)
    implementation(libs.androidx.activity)
    implementation(libs.androidx.lifecycle.runtime)
    implementation(libs.androidx.lifecycle.viewmodel)

    // 日志
    implementation(libs.timber)

    // 协程：Insta360 SDK 2.1.5 的 connect/activeCamera/startCapture/stopCapture 均为 suspend 函数
    implementation(libs.kotlinx.coroutines.android)

    // Insta360 SDK：sdk-camera 提供连接/激活/拍摄控制。
    // 注意：不引入 sdk-media。它传递依赖的 bmgmedia 会带进 libarvbmg.so(135MB)+libarffmpeg.so(13MB)
    // 等约 155MB 原生库，而本工程只做「实时触发拍摄」，不做相机文件下载/剪辑，
    // 引入后 APK 从 ~75MB 膨胀到 ~235MB，得不偿失。
    // 若后续要做事后文件同步，再单独打开 sdk-media（见 docs/方案说明.md）。
    implementation(libs.inskmp.camera)
}
