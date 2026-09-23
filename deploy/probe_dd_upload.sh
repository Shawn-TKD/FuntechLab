#!/bin/bash
S=/opt/daydreamer/project/src/daydreamer_agent/web/server.py
echo "=== 1) same_origin 实现 ==="
grep -n "def same_origin" -A 22 "$S"
echo ""
echo "=== 2) upload() 实现 ==="
grep -n "def upload" -A 50 "$S"
echo ""
echo "=== 3) MAX_UPLOAD / ID 常量 ==="
grep -n "MAX_UPLOAD\|^ID\|ID =" "$S" | head -20
echo ""
echo "=== 4) public_job 实现 ==="
grep -n "def public_job" -A 16 "$S"
