#!/bin/bash
# 在阿里云 ECS 上安装「心跳相机素材接收服务」
#
# 隔离原则（服务器上有队友在跑 VS Code Server / Qoder，不能互相影响）：
#   - 代码  /opt/heartbeat-upload/           专属目录
#   - 视频  /opt/heartbeat-upload/videos/    专属目录，按日期分子文件夹
#   - 日志  /var/log/heartbeat-upload.log    专属日志文件
#   - 服务  heartbeat-upload.service         专属 unit，只监听 8000
#   - 不装任何 pip 包（纯标准库）、不改系统 Python、不动别人任何配置
set -e

APP_DIR=/opt/heartbeat-upload
VIDEO_DIR=/opt/heartbeat-upload/videos

mkdir -p "$APP_DIR" "$VIDEO_DIR"
mv -f /root/upload_server.py "$APP_DIR/upload_server.py"
chmod 644 "$APP_DIR/upload_server.py"

cat > /etc/systemd/system/heartbeat-upload.service <<'UNIT'
[Unit]
Description=Insta360 Heartbeat Camera media upload receiver
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/heartbeat-upload
ExecStart=/usr/bin/python3 -u /opt/heartbeat-upload/upload_server.py --port 8000 --host :: --out /opt/heartbeat-upload/videos --by-date
Restart=always
RestartSec=3
StandardOutput=append:/var/log/heartbeat-upload.log
StandardError=append:/var/log/heartbeat-upload.log

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable heartbeat-upload.service >/dev/null 2>&1
systemctl restart heartbeat-upload.service
sleep 2

echo "--- is-active ---"
systemctl is-active heartbeat-upload.service
echo "--- listen 8000 ---"
ss -lntp | grep ':8000' || echo "NO_LISTEN_8000"
echo "--- health ---"
curl -s -m 5 --noproxy '*' http://127.0.0.1:8000/health || echo "HEALTH_FAIL"
echo ""
echo "--- 视频目录 ---"
ls -ld "$VIDEO_DIR"
echo "--- 磁盘 ---"
df -h / | tail -1
echo "--- recent log ---"
tail -n 12 /var/log/heartbeat-upload.log 2>/dev/null || true
