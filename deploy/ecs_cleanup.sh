#!/bin/bash
# 清掉验证用的测试文件
rm -f /opt/heartbeat-upload/videos/2026-09-23/LRV_20260923_TEST_01.mp4
echo "--- videos 目录 ---"
find /opt/heartbeat-upload/videos -type f | sort
echo "(空即表示已清理干净)"
echo "--- 服务仍在 ---"
systemctl is-active heartbeat-upload
