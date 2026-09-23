#!/bin/bash
# 只读：研究 daydreamer 的 /api/jobs 上传契约（照抄参考实现，不改动对方任何文件）
echo "===== 1) web/upload.js ====="
cat /opt/daydreamer/web/upload.js 2>/dev/null || echo "(missing)"

echo ""
echo "===== 2) web/connect.js ====="
cat /opt/daydreamer/web/connect.js 2>/dev/null || echo "(missing)"

echo ""
echo "===== 3) web/config.js ====="
cat /opt/daydreamer/web/config.js 2>/dev/null || echo "(missing)"

echo ""
echo "===== 4) web 目录清单 ====="
ls -la /opt/daydreamer/web/ 2>/dev/null

echo ""
echo "===== 5) server.py 里 jobs 出现的行 ====="
grep -n "jobs" /opt/daydreamer/project/src/daydreamer_agent/web/server.py 2>/dev/null | head -40

echo ""
echo "===== 6) server.py 里 连接码 / token / auth 相关 ====="
grep -n -iE "连接码|token|auth|cookie|session" /opt/daydreamer/project/src/daydreamer_agent/web/server.py 2>/dev/null | head -60

echo ""
echo "===== 7) server.py 文件规模 ====="
wc -l /opt/daydreamer/project/src/daydreamer_agent/web/server.py /opt/daydreamer/project/src/daydreamer_agent/web/store.py 2>/dev/null

echo ""
echo "===== 8) daydreamer-api.service 单元 ====="
cat /etc/systemd/system/daydreamer-api.service 2>/dev/null
