package com.insta360.heartbeat.camera

import android.content.Context
import android.net.ConnectivityManager
import android.net.Network
import android.net.NetworkCapabilities
import android.net.NetworkRequest
import kotlinx.coroutines.suspendCancellableCoroutine
import kotlinx.coroutines.withTimeoutOrNull
import kotlin.coroutines.resume

/**
 * 激活前把进程的网络绑定到「可上公网」的网络（蜂窝 / 家用路由器 WiFi），
 * 避免手机连在相机热点（无出口）时 activeCamera 因访问不了 openapi.insta360.com 而失败。
 *
 * SDK 2.1.5 的 activeCamera 是 suspend 函数，故这里也做成 suspend：
 *   1. 当前已有带 INTERNET 能力的默认网络 → 直接返回，调用方继续。
 *   2. 否则注册一次网络回调等待可用网络，最长等 [TIMEOUT_MS]；
 *      超时或注册失败也照样返回，把真实报错交给调用方去展示。
 */
object NanProcessNetworkBinding {

    private const val TIMEOUT_MS = 5_000L

    /** 挂起直到进程可上公网（或超时）。返回后调用方即可执行需要公网的操作。 */
    suspend fun awaitDefaultNetwork(context: Context) {
        val cm = context.getSystemService(Context.CONNECTIVITY_SERVICE) as ConnectivityManager
        val needCap = NetworkCapabilities.NET_CAPABILITY_INTERNET

        // 已经能上公网 → 立即返回
        val active = cm.activeNetwork
        if (active != null && cm.getNetworkCapabilities(active)?.hasCapability(needCap) == true) {
            return
        }

        withTimeoutOrNull(TIMEOUT_MS) {
            suspendCancellableCoroutine { cont ->
                val callback = object : ConnectivityManager.NetworkCallback() {
                    override fun onAvailable(network: Network) {
                        runCatching { cm.unregisterNetworkCallback(this) }
                        if (cont.isActive) cont.resume(Unit)
                    }
                }
                cont.invokeOnCancellation {
                    runCatching { cm.unregisterNetworkCallback(callback) }
                }
                runCatching {
                    cm.registerNetworkCallback(
                        NetworkRequest.Builder().addCapability(needCap).build(),
                        callback
                    )
                }.onFailure {
                    // 注册失败就直接放行，交由调用方报错
                    if (cont.isActive) cont.resume(Unit)
                }
            }
        }
    }
}
