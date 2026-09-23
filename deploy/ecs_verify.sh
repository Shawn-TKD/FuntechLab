#!/bin/bash
# ECS 侧验证：目录结构 + 隔离性
rm -rf /var/lib/heartbeat-upload
head -c 1000000 /dev/urandom > /tmp/t.bin

echo "--- upload test ---"
curl -s -m 60 --noproxy '*' -w "\nHTTP=%{http_code}\n" \
  -F "clip=layouttest" \
  -F "file=@/tmp/t.bin;filename=LRV_20260923_TEST_01.mp4;type=video/mp4" \
  http://127.0.0.1:8000/upload

echo "--- tree /opt/heartbeat-upload ---"
find /opt/heartbeat-upload | sort

echo "--- teammates processes still alive? ---"
ps -eo pid,comm --no-headers | grep -Ei 'code|Qoder|node' | head -6

echo "--- listening ports (excluding 8000) ---"
ss -lntp | grep -v ':8000' | tail -6

echo "--- disk ---"
df -h / | tail -1

rm -f /tmp/t.bin
