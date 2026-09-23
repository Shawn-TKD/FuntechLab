#!/bin/bash
echo "=== 1) service.env ==="
cat /etc/daydreamer/service.env 2>/dev/null || echo "(读取失败)"
echo ""
echo "=== 2) /etc/daydreamer 权限与文件 ==="
ls -l /etc/daydreamer/ 2>/dev/null
echo ""
echo "=== 3) server.py 130-200 行（鉴权与路由）==="
sed -n '130,200p' /opt/daydreamer/project/src/daydreamer_agent/web/server.py
echo ""
echo "=== 4) store.py 里 authorized 的实现 ==="
grep -n "def authorized" -A 14 /opt/daydreamer/project/src/daydreamer_agent/web/store.py
echo ""
echo "=== 5) server.py 里 upload_token 相关 ==="
grep -n "upload_token\|UPLOAD_TOKEN" /opt/daydreamer/project/src/daydreamer_agent/web/server.py
echo ""
echo "=== 6) 服务运行中的环境变量是否含令牌 ==="
tr '\0' '\n' < /proc/$(systemctl show -p MainPID --value daydreamer-api)/environ 2>/dev/null | grep -i daydreamer || echo "(读不到)"
echo ""
echo "=== 7) 服务状态 ==="
systemctl is-active daydreamer-api
systemctl show -p ActiveEnterTimestamp --value daydreamer-api
