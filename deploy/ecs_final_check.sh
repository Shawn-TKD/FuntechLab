#!/bin/bash
echo "--- service ---"
systemctl is-active heartbeat-upload
systemctl is-enabled heartbeat-upload
echo "--- videos tree ---"
find /opt/heartbeat-upload/videos -type f | head -20
echo "(none = empty)"
echo "--- recent requests (log) ---"
grep -E 'GET|来自' /var/log/heartbeat-upload.log | tail -8
echo "--- isolated paths ---"
ls -d /opt/heartbeat-upload /opt/heartbeat-upload/videos /var/log/heartbeat-upload.log
echo "--- disk ---"
df -h / | tail -1
