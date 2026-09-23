// 这是一个独立于 Insta360SDKDemo 的极简工程，只做一件事：
// 手机订阅 BLE 心率广播(0x180D) -> 阈值判定 -> 控制 Insta360 GO Ultra 录像。
// 依赖版本与 Insta360 Android SDK 2.1.5 官方 demo 保持一致。
//
// 说明：Insta360 官方 Maven 需要凭据。本工程刻意不把凭据硬编码进代码（官方 demo 是硬编码的），
// 改为从 gradle 属性读取，属性写在（git 忽略的）gradle.properties 或 ~/.gradle/gradle.properties：
//   insta360MavenUser=insta360guest
//   insta360MavenPassword=******
pluginManagement {
    repositories {
        google()
        mavenCentral()
        gradlePluginPortal()
        maven { url = uri("https://maven.aliyun.com/repository/public/") }
        maven { url = uri("https://maven.aliyun.com/repository/google/") }
        maven { url = uri("https://maven.aliyun.com/repository/gradle-plugin/") }
        maven {
            url = uri("https://androidsdk.insta360.com/repository/maven-public/")
            credentials {
                username = providers.gradleProperty("insta360MavenUser").orElse("").get()
                password = providers.gradleProperty("insta360MavenPassword").orElse("").get()
            }
        }
    }
}

dependencyResolutionManagement {
    repositoriesMode.set(RepositoriesMode.FAIL_ON_PROJECT_REPOS)
    repositories {
        google()
        mavenCentral()
        maven { url = uri("https://maven.aliyun.com/repository/public/") }
        maven { url = uri("https://maven.aliyun.com/repository/google/") }
        mavenLocal()
        maven { url = uri("https://jitpack.io") }
        maven {
            url = uri("https://androidsdk.insta360.com/repository/maven-public/")
            credentials {
                username = providers.gradleProperty("insta360MavenUser").orElse("").get()
                password = providers.gradleProperty("insta360MavenPassword").orElse("").get()
            }
        }
    }
}

rootProject.name = "heartbeat-camera"
include(":app")
