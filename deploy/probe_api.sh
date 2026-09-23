#!/bin/bash
# 只读探测：弄清 443 上那个服务是谁、怎么实现的、/api/jobs 的契约
echo "=== 1) 监听端口 ==="
ss -lntp | grep -E ':(80|443|8000|8443)\b' || echo "(none)"

echo "=== 2) nginx 是否存在 ==="
if command -v nginx >/dev/null 2>&1; then nginx -v 2>&1; else echo "no nginx binary"; fi

echo "=== 3) nginx 配置 ==="
for f in /etc/nginx/sites-enabled/* /etc/nginx/conf.d/*.conf; do
  if [ -f "$f" ]; then echo "--- $f ---"; cat "$f"; fi
done

echo "=== 4) docker 容器 ==="
if command -v docker >/dev/null 2>&1; then
  docker ps --format '{{.Names}} | {{.Image}} | {{.Ports}}' 2>&1 || echo "docker ps failed"
else
  echo "no docker"
fi

echo "=== 5) 443 是哪个进程 ==="
ss -lntp 2>/dev/null | grep ':443' || echo "(no 443 listener visible)"

echo "=== 6) 自定义 systemd 服务 ==="
ls -1 /etc/systemd/system/*.service 2>/dev/null | grep -v -E 'multi-user|getty' || true

echo "=== 7) /opt 与 /srv 目录 ==="
ls -la /opt 2>/dev/null
ls -la /srv 2>/dev/null

echo "=== 8) 443 上的自签证书 ==="
echo | timeout 5 openssl s_client -connect 127.0.0.1:443 2>/dev/null | openssl x509 -noout -subject -issuer -dates 2>/dev/null || echo "(openssl 读取失败)"

echo "=== 9) 本机访问 / 看重定向 ==="
curl -sk -m 8 -o /dev/null -w 'GET / -> HTTP=%{http_code} LOCATION=%{redirect_url}\n' https://127.0.0.1/ 2>&1 || echo "curl failed"

echo "=== 10) 本机访问 /api/jobs ==="
curl -sk -m 8 -w '\nHTTP=%{http_code}\n' https://127.0.0.1/api/jobs 2>&1 || echo "curl failed"

echo "=== 11) 全盘找含 api/jobs 的文件（限深度，排除系统目录）==="
grep -rl "api/jobs" /opt /srv /root /home /etc/nginx 2>/dev/null | head -20 || echo "(no match)"
grep -rl "连接码" /opt /srv /root /home 2>/dev/null | head -20 || echo "(no match)"
