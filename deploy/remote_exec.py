# -*- coding: utf-8 -*-
"""
远程执行工具 —— 通过 SSH 在目标服务器上跑命令 / 传文件。

用法：
    python remote_exec.py "uname -a"                    # 跑一条命令
    python remote_exec.py --file scripts/setup.sh       # 跑一个本地脚本
    python remote_exec.py --put local.txt /root/        # 上传文件
    python remote_exec.py --get /root/a.log ./a.log     # 下载文件

密码来源（按优先级）：
    1) 环境变量  YSSH_PWD
    2) 同目录下的  .ssh_pass  文件（一行明文；请勿提交到 git）
    3) 已配置 SSH 密钥时：设  YSSH_KEY=1  走密钥认证（推荐，无需明文密码）

依赖：paramiko（已装在 E:\\workbuddy_venv）
"""
import argparse
import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import paramiko

HOST = os.environ.get("YSSH_HOST", "your-server.example.com")
PORT = int(os.environ.get("YSSH_PORT", "22"))
USER = os.environ.get("YSSH_USER", "root")
HERE = os.path.dirname(os.path.abspath(__file__))
PASS_FILE = os.path.join(HERE, ".ssh_pass")


def resolve_credential():
    """返回 (password, key_filename)；两者都 None 表示认证信息缺失。"""
    if os.environ.get("YSSH_KEY") == "1":
        for name in ("id_ed25519", "id_rsa"):
            p = os.path.join(os.path.expanduser("~"), ".ssh", name)
            if os.path.isfile(p):
                return None, p
        raise SystemExit("YSSH_KEY=1 但未找到 ~/.ssh/id_ed25519 或 id_rsa")
    pwd = os.environ.get("YSSH_PWD")
    if pwd:
        return pwd, None
    if os.path.isfile(PASS_FILE):
        with open(PASS_FILE, encoding="utf-8") as f:
            return f.read().strip(), None
    raise SystemExit(
        "缺少认证信息。请设置环境变量 YSSH_PWD，或在 %s 写入一行密码，"
        "或配置 SSH 密钥后设 YSSH_KEY=1。" % PASS_FILE
    )


def connect():
    pwd, key = resolve_credential()
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(HOST, port=PORT, username=USER, password=pwd, key_filename=key,
              timeout=25, banner_timeout=25, auth_timeout=25,
              look_for_keys=False, allow_agent=False)
    return c


def run(client, script):
    _i, out, err = client.exec_command(script, timeout=1800)
    o = out.read().decode("utf-8", "replace")
    e = err.read().decode("utf-8", "replace")
    rc = out.channel.recv_exit_status()
    if o:
        print(o, end="" if o.endswith("\n") else "\n")
    if e.strip():
        print("--- stderr ---", file=sys.stderr)
        print(e, end="" if e.endswith("\n") else "\n", file=sys.stderr)
    return rc


def main():
    ap = argparse.ArgumentParser(description="SSH 远程执行工具")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("command", nargs="?", help="要执行的命令")
    g.add_argument("--file", help="本地脚本文件（内容原样作为远程脚本执行）")
    g.add_argument("--put", nargs=2, metavar=("LOCAL", "REMOTE"), help="上传文件")
    g.add_argument("--get", nargs=2, metavar=("REMOTE", "LOCAL"), help="下载文件")
    ap.add_argument("--quiet", action="store_true", help="不打印退出码")
    args = ap.parse_args()

    client = connect()
    try:
        if args.put:
            sftp = client.open_sftp()
            sftp.put(args.put[0], args.put[1])
            print("已上传 %s -> %s:%s" % (args.put[0], HOST, args.put[1]))
            sftp.close()
            return 0
        if args.get:
            sftp = client.open_sftp()
            sftp.get(args.get[0], args.get[1])
            print("已下载 %s:%s -> %s" % (HOST, args.get[0], args.get[1]))
            sftp.close()
            return 0

        script = args.command
        if args.file:
            with open(args.file, encoding="utf-8") as f:
                script = f.read()
        rc = run(client, script)
        if not args.quiet:
            print("[exit=%d]" % rc)
        return rc
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(main())
