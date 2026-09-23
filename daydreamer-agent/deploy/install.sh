#!/bin/bash
set -euo pipefail
test -f /opt/daydreamer/project/daydreamer.py
id daydreamer >/dev/null 2>&1 || useradd --system --home-dir /var/lib/daydreamer --shell /usr/sbin/nologin daydreamer
install -d -o daydreamer -g daydreamer -m 700 /var/lib/daydreamer /opt/daydreamer/project/data /opt/daydreamer/project/outputs
install -d -m 700 /etc/daydreamer
install -o root -g root -m 600 /root/daydreamer-service.env /etc/daydreamer/service.env
python3 -m venv /opt/daydreamer/venv
python3 - <<'PY'
from pathlib import Path
p = Path('/opt/daydreamer/project/config/default.toml')
s = p.read_text()
s = '\n'.join('csv_path = ""' if line.startswith('csv_path =') else line for line in s.splitlines()) + '\n'
p.write_text(s)
PY
install -m 644 /opt/daydreamer/deploy/daydreamer-*.service /etc/systemd/system/
install -m 644 /opt/daydreamer/deploy/daydreamer-*.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now daydreamer-api daydreamer-worker
systemctl is-active daydreamer-api daydreamer-worker
curl --fail --silent http://127.0.0.1:8765/healthz
echo
