#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
心跳相机 App 的素材接收服务（最小可用版，仅用 Python 标准库，无需 pip install）。

用途：验证 App 上「上传最新一段（LRV → MP4）」这个按钮的完整链路。
后续要集成到别的产品里时，把 handle_upload() 换成你的业务逻辑即可 ——
协议就一个 multipart POST：
    POST /upload
    fields: source / clip / sizeBytes / clientTime
    file:   file=<filename.mp4>  (video/mp4)

用法：
    python upload_server.py --port 8000 --out "E:\\心跳相机上传"
    # 默认输出到脚本同级的 received/ 目录

配合手机做端到端测试（电脑插着 USB 线即可，不要求同一局域网）：
    adb reverse tcp:8000 tcp:8000
    然后把 App 里的上传地址填成  http://127.0.0.1:8000/upload

健康检查：
    curl http://127.0.0.1:8000/health
"""

import argparse
import email
import hashlib
import json
import mimetypes
import os
import re
import socket
import sys
import time
import urllib.parse
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DEFAULT_OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "received")
MAX_BYTES = 2 * 1024 * 1024 * 1024  # 2GB 上限，防止误传把磁盘写满

STATE = {"out_dir": DEFAULT_OUT, "counter": 0, "by_date": False}


def human_size(n: int) -> str:
    """字节数转成人看的字符串。"""
    f = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if f < 1024 or unit == "TB":
            return ("%d %s" % (f, unit)) if unit == "B" else ("%.2f %s" % (f, unit))
        f /= 1024
    return "%d B" % n


# 浏览器打开 http://<服务器>:8000/ 就能看到这个页面 —— 按文件夹分组的素材清单
INDEX_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>心跳相机 · 服务器素材</title>
<style>
 *{box-sizing:border-box}
 body{margin:0;font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;
      background:#f5f6f8;color:#1f2328}
 header{background:#fff;border-bottom:1px solid #e5e7eb;padding:16px 22px;display:flex;
        align-items:center;gap:14px;flex-wrap:wrap;position:sticky;top:0;z-index:5}
 h1{font-size:17px;margin:0;font-weight:600}
 .stats{color:#6b7280;font-size:13px}
 .path{color:#9ca3af;font-size:12px;font-family:ui-monospace,Menlo,Consolas,monospace}
 button{border:1px solid #d1d5db;background:#fff;border-radius:7px;padding:6px 13px;
        font-size:13px;cursor:pointer;font-family:inherit}
 button:hover{background:#f3f4f6}
 a{text-decoration:none}
 main{padding:18px 22px 60px;max-width:1080px;margin:0 auto}
 .group{background:#fff;border:1px solid #e5e7eb;border-radius:10px;margin-bottom:16px;overflow:hidden}
 .ghead{padding:11px 15px;background:#fafbfc;border-bottom:1px solid #eef0f2;font-size:13px;
        font-weight:600;display:flex;justify-content:space-between;align-items:center}
 .ghead .n{color:#6b7280;font-weight:400}
 .file{padding:11px 15px;border-bottom:1px solid #f3f4f6;display:flex;align-items:center;gap:12px}
 .file:last-child{border-bottom:none}
 .file:hover{background:#fafbfc}
 .nm{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:13.5px;
     font-family:ui-monospace,Menlo,Consolas,monospace}
 .meta{color:#6b7280;font-size:12px;white-space:nowrap}
 .empty{text-align:center;color:#9ca3af;padding:70px 20px;font-size:14px;line-height:1.9}
 dialog{border:none;border-radius:12px;padding:0;max-width:92vw;max-height:92vh}
 dialog::backdrop{background:rgba(0,0,0,.6)}
 dialog video{display:block;max-width:88vw;max-height:78vh;background:#000}
 dialog .bar{padding:9px 13px;display:flex;justify-content:space-between;align-items:center;
             gap:12px;font-size:13px;background:#fff}
</style>
</head>
<body>
<header>
  <h1>心跳相机 · 服务器素材</h1>
  <div>
    <div class="stats" id="stats">加载中…</div>
    <div class="path" id="root"></div>
  </div>
  <div style="flex:1"></div>
  <button onclick="load()">刷新</button>
</header>
<main id="main"><div class="empty">加载中…</div></main>
<dialog id="dlg">
  <div class="bar"><span id="dname"></span><button onclick="closeDlg()">关闭</button></div>
  <video id="dvid" controls playsinline></video>
</dialog>
<script>
function esc(s){return String(s).replace(/[&<>"']/g,function(c){
  return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];});}
function closeDlg(){
  var v=document.getElementById('dvid');
  v.pause(); v.removeAttribute('src'); v.load();
  document.getElementById('dlg').close();
}
function play(url){
  var name=decodeURIComponent(url.split('/').pop());
  document.getElementById('dname').textContent=name;
  var v=document.getElementById('dvid');
  v.src=url;
  document.getElementById('dlg').showModal();
  v.play().catch(function(){});
}
function load(){
  var main=document.getElementById('main');
  fetch('/list',{cache:'no-store'}).then(function(r){return r.json();}).then(function(d){
    document.getElementById('stats').textContent=
      d.totalCount+' 个文件 · '+d.totalSizeText+' · '+d.groupCount+' 个文件夹';
    document.getElementById('root').textContent=d.root;
    if(!d.totalCount){
      main.innerHTML='<div class="empty">服务器上还没有视频。<br>先在 App 里「① 刷新素材列表」→ 选一条 →「② 上传选中素材」。</div>';
      return;
    }
    main.innerHTML=d.groups.map(function(g){
      return '<div class="group"><div class="ghead"><span>文件夹 ' + esc(g.folder) +
        '</span><span class="n">' + g.count + ' 个文件</span></div>' +
        g.files.map(function(f){
          return '<div class="file"><span class="nm" title="' + esc(f.relPath) + '">' +
            esc(f.name) + '</span><span class="meta">' + f.sizeText +
            '</span><span class="meta">' + esc(f.mtime) + '</span>' +
            '<button onclick="play(\'' + f.url + '\')">播放</button>' +
            '<a href="' + f.url + '" download><button>下载</button></a></div>';
        }).join('') + '</div>';
    }).join('');
  }).catch(function(e){
    main.innerHTML='<div class="empty">读取失败：' + esc(e.message) + '</div>';
  });
}
load();
</script>
</body>
</html>
"""


def sanitize(name: str) -> str:
    """只保留文件名部分，去掉路径分隔符与危险字符。"""
    name = os.path.basename(name or "").replace("\\", "_").replace("/", "_")
    name = re.sub(r"[^\w\u4e00-\u9fff.\-]+", "_", name)
    return name or "unnamed.bin"


def parse_multipart(body: bytes, content_type: str):
    """用标准库 email 解析 multipart/form-data，返回 (files, fields)。"""
    header = ("Content-Type: %s\r\nMIME-Version: 1.0\r\n\r\n" % content_type).encode("utf-8", "replace")
    msg = email.message_from_bytes(header + body)
    files, fields = [], {}
    if not msg.is_multipart():
        return files, fields
    for part in msg.walk():
        if part.get_content_maintype() == "multipart":
            continue
        disp = part.get("Content-Disposition", "") or ""
        if "form-data" not in disp.lower():
            continue
        name = part.get_param("name", header="content-disposition")
        filename = part.get_filename()
        payload = part.get_payload(decode=True) or b""
        if filename:
            files.append((name, filename, payload, part.get_content_type()))
        else:
            fields[name or "?"] = payload.decode("utf-8", "replace")
    return files, fields


class Handler(BaseHTTPRequestHandler):
    server_version = "HeartbeatUpload/1.1"

    # ⚠️ 必须是 HTTP/1.1，否则 BaseHTTPRequestHandler 会跳过 Expect 处理，
    # 大文件上传（okhttp 会带 Expect: 100-continue）容易卡住。
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # 默认日志太吵，自己控制
        pass

    def _sock_name(self) -> str:
        """访问方地址。打印出来最有用：能直接区分「来自手机的 adb reverse」和「本机自测」。"""
        try:
            return "%s:%s" % self.client_address[:2]
        except Exception:  # noqa: BLE001
            return "?"

    def _send_json(self, code: int, payload: dict):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        # 每次 GET 都打一行 —— 手机端 adb reverse 通不通，看这行就知道
        print("[%s] ← GET %s（来自 %s）" % (
            datetime.now().strftime("%H:%M:%S"), self.path, self._sock_name()))
        path = urllib.parse.urlparse(self.path).path
        if path.startswith("/health"):
            self._send_json(200, {"ok": True, "outDir": STATE["out_dir"], "received": STATE["counter"]})
        elif path.startswith("/list"):
            self._serve_list()
        elif path.startswith("/videos/"):
            self._serve_video(path)
        elif path in ("/", "/index.html"):
            self._send_index()
        else:
            self._send_json(404, {
                "ok": False,
                "error": "支持 GET /（网页浏览）、/health、/list（JSON 素材清单）、/videos/<文件夹>/<文件名>",
            })

    # ---------- 素材浏览：网页 + JSON + 文件流 ----------

    def _file_entry(self, full_path: str, folder: str) -> dict:
        st = os.stat(full_path)
        filename = os.path.basename(full_path)
        rel = ("%s/%s" % (folder, filename)) if folder else filename
        return {
            "name": filename,
            "folder": folder or "(未分类)",
            "relPath": rel,
            "size": st.st_size,
            "sizeText": human_size(st.st_size),
            "mtime": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
            "mtimeRaw": st.st_mtime,
            "url": "/videos/" + urllib.parse.quote(rel),
        }

    def _scan_videos(self):
        """扫描保存目录，返回 (groups, 文件总数, 总字节数)。

        开了 --by-date 时一个日期就是一个分组；顶层散落的文件归到「(未分类)」。
        """
        root = STATE["out_dir"]
        groups, loose, total_count, total_bytes = [], [], 0, 0
        try:
            names = sorted(os.listdir(root), reverse=True)
        except OSError:
            names = []
        for name in names:
            full = os.path.join(root, name)
            if os.path.isdir(full):
                try:
                    subnames = sorted(os.listdir(full), reverse=True)
                except OSError:
                    subnames = []
                files = [self._file_entry(os.path.join(full, fn), name)
                         for fn in subnames if os.path.isfile(os.path.join(full, fn))]
                if files:
                    groups.append({"folder": name, "count": len(files), "files": files})
                    total_count += len(files)
                    total_bytes += sum(f["size"] for f in files)
            elif os.path.isfile(full):
                loose.append(self._file_entry(full, ""))
        if loose:
            loose.sort(key=lambda f: f["mtimeRaw"], reverse=True)
            groups.append({"folder": "(未分类)", "count": len(loose), "files": loose})
            total_count += len(loose)
            total_bytes += sum(f["size"] for f in loose)
        return groups, total_count, total_bytes

    def _serve_list(self):
        groups, total_count, total_bytes = self._scan_videos()
        self._send_json(200, {
            "ok": True,
            "root": os.path.abspath(STATE["out_dir"]),
            "totalCount": total_count,
            "totalBytes": total_bytes,
            "totalSizeText": human_size(total_bytes),
            "groupCount": len(groups),
            "groups": groups,
            "serverTime": datetime.now().isoformat(timespec="seconds"),
        })

    def _send_index(self):
        body = INDEX_HTML.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _serve_video(self, path: str):
        """按路径把保存目录里的文件流出去（支持 Range，可拖动播放进度）。"""
        rel = urllib.parse.unquote(path[len("/videos/"):])
        root = os.path.realpath(STATE["out_dir"])
        target = os.path.realpath(os.path.join(root, rel))
        # 防目录穿越：解析后必须仍在保存目录内
        if not (target == root or target.startswith(root + os.sep)) or not os.path.isfile(target):
            self._send_json(404, {"ok": False, "error": "文件不存在：%s" % rel})
            return

        size = os.path.getsize(target)
        ctype = mimetypes.guess_type(target)[0] or "application/octet-stream"
        start, end, partial = 0, max(size - 1, 0), False
        rng = self.headers.get("Range")
        if rng:
            m = re.match(r"bytes=(\d*)-(\d*)", rng.strip())
            if m and (m.group(1) or m.group(2)):
                if m.group(1):
                    start = int(m.group(1))
                if m.group(2):
                    end = min(int(m.group(2)), size - 1)
                if start >= size or start > end:
                    self.send_response(416)
                    self.send_header("Content-Range", "bytes */%d" % size)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                partial = True

        length = end - start + 1
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        if partial:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        self.end_headers()
        with open(target, "rb") as fp:
            fp.seek(start)
            remaining = length
            while remaining > 0:
                chunk = fp.read(min(remaining, 1 << 20))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def do_POST(self):
        try:
            self._handle_post()
        except Exception:  # noqa: BLE001
            # 兜底：任何未预期异常都要回一个响应，否则客户端只会看到
            # 「unexpected end of stream」，完全无法定位。
            import traceback
            tb = traceback.format_exc()
            sys.stderr.write("[do_POST 异常]\n%s\n" % tb)
            sys.stderr.flush()
            try:
                self._send_json(500, {"ok": False, "error": "服务端内部错误", "trace": tb[-800:]})
            except Exception:  # noqa: BLE001
                pass

    def _handle_post(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            self._send_json(400, {"ok": False, "error": "缺少 Content-Length"})
            return
        if length > MAX_BYTES:
            self._send_json(413, {"ok": False, "error": "超过 %d 字节上限" % MAX_BYTES})
            return

        ctype = self.headers.get("Content-Type", "")
        started = time.time()
        # ⚠️ 必须用 _sock_name()：AF_INET6 双栈下 client_address 是四元组
        # (host, port, flowinfo, scope_id)，直接 "%s:%s" % self.client_address
        # 会抛 TypeError: not all arguments converted during string formatting，
        # 请求体一个字节都读不到就断连（v13 上传失败的真正根因）。
        peer = self._sock_name()
        print("[%s] ← 来自 %s 的请求，%.2f MB" % (
            datetime.now().strftime("%H:%M:%S"), peer, length / 1048576.0))

        body = self._read_body(length)
        if body is None:
            return

        if "multipart/form-data" not in ctype.lower():
            self._send_json(400, {"ok": False, "error": "只接受 multipart/form-data，收到：%s" % ctype})
            return

        try:
            files, fields = parse_multipart(body, ctype)
        except Exception as exc:  # noqa: BLE001
            self._send_json(400, {"ok": False, "error": "解析 multipart 失败：%r" % exc})
            return

        if not files:
            self._send_json(400, {"ok": False, "error": "请求里没有文件字段（应叫 file）", "fields": fields})
            return

        saved = []
        for field_name, filename, payload, mime in files:
            safe = sanitize(filename)
            target = self._unique_path(safe)
            with open(target, "wb") as fp:
                fp.write(payload)
            digest = hashlib.sha256(payload).hexdigest()
            STATE["counter"] += 1
            saved.append({
                "field": field_name,
                "file": os.path.abspath(target),
                "bytes": len(payload),
                "sha256": digest,
                "contentType": mime,
            })
            print("   ✓ 已保存 %s（%.2f MB, sha256=%s…）" % (
                os.path.basename(target), len(payload) / 1048576.0, digest[:12]))

        if fields:
            print("   表单字段：%s" % json.dumps(fields, ensure_ascii=False))
        print("   耗时 %.2fs" % (time.time() - started))

        self._send_json(200, {
            "ok": True,
            "received": STATE["counter"],
            "saved": saved,
            "fields": fields,
            "serverTime": datetime.now().isoformat(timespec="seconds"),
        })

    def _read_body(self, length: int):
        buf = bytearray()
        remaining = length
        while remaining > 0:
            chunk = self.rfile.read(min(remaining, 1 << 20))
            if not chunk:
                self._send_json(400, {"ok": False, "error": "连接中断，只收到 %d 字节" % len(buf)})
                return None
            buf.extend(chunk)
            remaining -= len(chunk)
        return bytes(buf)

    def _unique_path(self, name: str) -> str:
        out_dir = STATE["out_dir"]
        # 按上传日期分子目录（YYYY-MM-DD），素材不会全部堆在一个目录里
        if STATE.get("by_date"):
            out_dir = os.path.join(out_dir, datetime.now().strftime("%Y-%m-%d"))
        os.makedirs(out_dir, exist_ok=True)
        target = os.path.join(out_dir, name)
        if not os.path.exists(target):
            return target
        stem, ext = os.path.splitext(name)
        stamp = datetime.now().strftime("%H%M%S")
        return os.path.join(out_dir, "%s_%s%s" % (stem, stamp, ext))


def make_server(host: str, port: int, handler):
    """建监听。

    ⚠️ 为什么需要这个函数：`adb reverse tcp:8000 tcp:8000` 在**宿主机侧**是往
    `localhost:8000` 建连的，而 Windows 上 `localhost` 常常优先解析到 IPv6 的 `::1`。
    如果服务只监听 IPv4 的 `127.0.0.1`，就会出现「手机端 TCP 连上了、却一个字节都收不到」
    —— 手机端表现为请求超时/空响应，非常难查。

    所以：host 里带 `:`（IPv6）时用 AF_INET6 并**关掉 V6ONLY** 走双栈，
    这样 `127.0.0.1`、`::1`、局域网 IPv4 三种访问方式同时可用。
    """
    if ":" not in host:
        return ThreadingHTTPServer((host, port), handler)

    class DualStackServer(ThreadingHTTPServer):
        address_family = socket.AF_INET6

        def server_bind(self):
            try:
                self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
            except OSError:
                pass  # 系统不支持双栈时忽略，退化成纯 IPv6
            super().server_bind()

    return DualStackServer((host, port), handler)


def main():
    ap = argparse.ArgumentParser(description="心跳相机素材接收服务")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument(
        "--host", default="::",
        help="监听地址。默认 :: （IPv6 双栈，同时接受 127.0.0.1 / ::1 / 局域网 IPv4）。"
             "只想要 IPv4 就传 0.0.0.0",
    )
    ap.add_argument("--out", default=DEFAULT_OUT, help="文件保存目录，默认 ./received")
    ap.add_argument(
        "--by-date",
        action="store_true",
        help="在保存目录下按上传日期建子目录（YYYY-MM-DD），避免素材全部堆在一个目录里",
    )
    args = ap.parse_args()

    STATE["out_dir"] = os.path.abspath(args.out)
    STATE["by_date"] = bool(args.by_date)
    os.makedirs(STATE["out_dir"], exist_ok=True)

    print("=" * 68)
    print("心跳相机素材接收服务已启动")
    print("  监听      : http://%s:%d" % (args.host, args.port))
    print("  接收地址  : POST http://<本机IP>:%d/upload" % args.port)
    print("  保存目录  : %s" % STATE["out_dir"])
    if STATE["by_date"]:
        print("  目录结构  : 按日期分子目录（%s/YYYY-MM-DD/xxx.mp4）" % STATE["out_dir"])
    print("  本机测试  : curl http://127.0.0.1:%d/health" % args.port)
    print("  手机联调  : adb reverse tcp:%d tcp:%d" % (args.port, args.port))
    print("              App 上传地址填 http://127.0.0.1:%d/upload" % args.port)
    print("=" * 68)
    print("等待上传…（Ctrl+C 退出）")

    httpd = make_server(args.host, args.port, Handler)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。共接收 %d 个文件 → %s" % (STATE["counter"], STATE["out_dir"]))
    finally:
        httpd.server_close()


if __name__ == "__main__":
    sys.exit(main())
