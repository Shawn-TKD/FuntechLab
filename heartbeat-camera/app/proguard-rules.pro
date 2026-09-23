# 影石 SDK 依赖的类，保持原始签名（retrofit/okhttp/gson 也常有此类问题）。
-keep class com.arashivision.** { *; }
-keep class com.insta360.** { *; }
-keepattributes Signature
-keepattributes *Annotation*
-dontwarn com.arashivision.**
-dontwarn okhttp3.**
-dontwarn retrofit2.**
-dontwarn okio.**
