"""Loopback-only API behind a TLS reverse proxy; no third-party dependencies."""
import hmac
import json
import mimetypes
import os
import re
import shutil
import sqlite3
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

from daydreamer_agent.web.store import Store, public_job

MAX_UPLOAD = 250 * 1024 * 1024
ID = r"[a-f0-9]{32}"
TYPES = {"video/mp4": ".mp4", "video/quicktime": ".mov", "video/webm": ".webm", "video/x-matroska": ".mkv", "video/3gpp": ".3gp"}


class APIError(Exception):
    def __init__(self, status, message):
        self.status, self.message = status, message


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, data, web, project, access_code, upload_token=""):
        if len(access_code) < 20:
            raise ValueError("Use a randomly generated access code of at least 20 characters")
        self.store = Store(data)
        self.web = Path(web).resolve()
        self.project = Path(project).resolve()
        self.access_code = access_code
        if upload_token and len(upload_token) < 32:
            raise ValueError("Use a randomly generated upload token of at least 32 characters")
        self.upload_token = upload_token
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    # HTTP/1.0 closes each response, including a rejected upload with unread bytes.
    server_version = "Daydreamer"

    def log_message(self, format, *args):
        pass  # Neither access codes nor uploaded names belong in access logs.

    def setup(self):
        super().setup()
        self.connection.settimeout(120)

    def reply(self, code, data, headers=None):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def length(self, maximum):
        if self.headers.get("Transfer-Encoding"):
            raise APIError(400, "请使用明确长度的请求。")
        try:
            size = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise APIError(400, "文件大小无效。")
        if size <= 0 or size > maximum:
            raise APIError(413, "文件为空或超过大小限制。")
        return size

    def json_body(self):
        if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
            raise APIError(415, "需要 JSON 请求。")
        try:
            value = json.loads(self.rfile.read(self.length(4096)))
            if not isinstance(value, dict):
                raise ValueError()
            return value
        except (ValueError, UnicodeError):
            raise APIError(400, "请求格式无效。")

    def authorized(self):
        bearer = self.headers.get("Authorization", "")
        if bearer.startswith("Bearer "):
            return self.server.store.authorized(bearer[7:])
        cookies = SimpleCookie()
        try:
            cookies.load(self.headers.get("Cookie", ""))
            token = cookies.get("daydreamer_session")
            return bool(token and self.server.store.authorized(token.value))
        except Exception:
            return False

    def same_origin(self):
        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            raise APIError(403, "请从本站提交请求。")
        origin = self.headers.get("Origin")
        if origin and urlsplit(origin).netloc != self.headers.get("Host"):
            raise APIError(403, "请求来源不匹配。")

    def do_HEAD(self):
        self.handle_request()

    def do_GET(self):
        self.handle_request()

    def do_POST(self):
        self.handle_request()

    def handle_request(self):
        try:
            self.route()
        except APIError as exc:
            self.reply(exc.status, {"error": exc.message})
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass
        except Exception:
            self.reply(500, {"error": "服务暂时不可用，请稍后重试。"})

    def route(self):
        path = urlsplit(self.path).path
        store = self.server.store
        if self.command == "POST":
            self.same_origin()
        if path == "/healthz" and self.command in {"GET", "HEAD"}:
            return self.reply(200, {"status": "ok"})
        if path == "/api/session" and self.command == "POST":
            code = self.json_body().get("code", "")
            if not isinstance(code, str) or not hmac.compare_digest(code.encode(), self.server.access_code.encode()):
                # Login attempts are additionally rate-limited by nginx.
                time.sleep(0.5)
                raise APIError(401, "连接码不正确。")
            secure = "; Secure" if self.headers.get("X-Forwarded-Proto") == "https" else ""
            if self.authorized():
                return self.reply(200, {"connected": True})
            try:
                token = store.session()
            except ValueError as exc:
                raise APIError(409, str(exc))
            cookie = "daydreamer_session=" + token + "; HttpOnly; SameSite=Strict; Path=/; Max-Age=2592000" + secure
            return self.reply(200, {"connected": True}, {"Set-Cookie": cookie})
        if path.startswith("/api/"):
            # Viewing is intentionally login-free. Mutations that create/resume
            # paid work and the full task library still require credentials.
            public_playback = (
                (path == "/api/videos/next" and self.command == "GET") or
                (re.fullmatch("/api/jobs/" + ID, path) and self.command == "GET") or
                (re.fullmatch("/api/jobs/" + ID + "/video", path) and self.command in {"GET", "HEAD"}) or
                (re.fullmatch("/api/jobs/" + ID + "/played", path) and self.command == "POST")
            )
            bearer = self.headers.get("Authorization", "")
            upload_access = bool(self.server.upload_token and bearer.startswith("Bearer ") and
                                 hmac.compare_digest(bearer[7:].encode(), self.server.upload_token.encode()))
            if upload_access and not public_playback:
                # The phone's uploader is independent of its browser session. It
                # may submit a recording and check a known job, but cannot bind
                # a browser, list the library, fetch videos or restart paid work.
                if path == "/api/jobs" and self.command == "POST":
                    return self.upload()
                if re.fullmatch("/api/jobs/" + ID, path) and self.command == "GET":
                    job = store.get(path.rsplit("/", 1)[-1])
                    if not job:
                        raise APIError(404, "任务不存在。")
                    return self.reply(200, public_job(job))
                raise APIError(403, "上传凭据仅可上传视频及查询已知任务。")
            if not public_playback and not self.authorized():
                raise APIError(401, "请先输入连接码。")
            if path == "/api/session" and self.command == "GET":
                return self.reply(200, {"connected": True, "max_upload_bytes": MAX_UPLOAD})
            if path == "/api/jobs" and self.command == "POST":
                return self.upload()
            if path == "/api/jobs" and self.command == "GET":
                return self.reply(200, {"jobs": [public_job(j) for j in store.list()]})
            if path == "/api/videos/next" and self.command == "GET":
                job = store.next()
                return self.reply(200, {"job": public_job(job) if job else None})
            match = re.fullmatch("/api/jobs/(" + ID + r")(/video|/played|/resume)?", path)
            if match:
                job = store.get(match[1])
                if not job:
                    raise APIError(404, "任务不存在。")
                action = match[2]
                if action is None and self.command == "GET":
                    return self.reply(200, public_job(job))
                if action == "/video" and self.command in {"GET", "HEAD"}:
                    if job["state"] != "ready" or not job["video"]:
                        raise APIError(409, "片段尚未准备好。")
                    video = Path(job["video"]).resolve()
                    if not video.is_relative_to(self.server.project / "outputs"):
                        raise APIError(404, "片段不存在。")
                    return self.file(video, "video/mp4", private=True)
                if action == "/played" and self.command == "POST":
                    if job["state"] != "ready":
                        raise APIError(409, "片段尚未准备好。")
                    store.update(job["id"], played=1)
                    return self.reply(200, {"ok": True})
                if action == "/resume" and self.command == "POST":
                    if not store.resume(job["id"]):
                        raise APIError(409, "该任务正在运行或已经完成。")
                    return self.reply(202, public_job(store.get(job["id"])))
            raise APIError(404, "接口不存在。")
        if self.command not in {"GET", "HEAD"}:
            raise APIError(405, "请求方法不支持。")
        if path in {"/upload", "/upload.html", "/connect", "/connect.html"}:
            self.send_response(303)
            self.send_header("Location", "/")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        # The landing page contains only the character/cloud UI. Show it on a
        # direct visit, even when a Strict cookie is absent on external navigation.
        # Paid job creation and administration remain protected in /api/.
        name = {"/": "index.html", "/upload": "upload.html", "/connect": "connect.html"}.get(path, unquote(path).lstrip("/"))
        file = (self.server.web / name).resolve()
        if not file.is_relative_to(self.server.web) or file.suffix.lower() not in {".html", ".js", ".css", ".png", ".svg", ".ico"}:
            raise APIError(404, "页面不存在。")
        return self.file(file, mimetypes.guess_type(str(file))[0] or "application/octet-stream")

    def upload(self):
        size = self.length(MAX_UPLOAD)
        content_type = self.headers.get("Content-Type", "").split(";")[0]
        if content_type not in TYPES:
            raise APIError(415, "请选择 MP4、MOV、WebM、MKV 或 3GP 视频。")
        key = self.headers.get("Idempotency-Key", "")
        if not re.fullmatch(r"[A-Za-z0-9_-]{16,100}", key):
            raise APIError(400, "缺少有效的上传请求编号。")
        store = self.server.store
        old = store.by_key(key)
        if old:
            if old["state"] == "uploading":
                raise APIError(409, "该视频仍在上传，请稍后重试。")
            return self.reply(200, public_job(old))
        if shutil.disk_usage(store.root).free < size + 2 * 1024**3:
            raise APIError(507, "服务器空间不足，请先整理已有素材。")
        try:
            job = store.reserve(key, TYPES[content_type])
        except sqlite3.IntegrityError:
            raise APIError(409, "同一请求正在上传，请稍后重试。")
        except ValueError as exc:
            raise APIError(429, str(exc))
        part = Path(job["source"] + ".part")
        try:
            remaining = size
            with part.open("xb") as file:
                while remaining:
                    chunk = self.rfile.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise APIError(400, "视频上传中断，请重新上传。")
                    file.write(chunk)
                    remaining -= len(chunk)
                file.flush()
                os.fsync(file.fileno())
            part.replace(job["source"])
            store.update(job["id"], state="queued", message="上传完成，等待分析视频")
        except BaseException:
            store.upload_failed(job["id"])
            raise
        return self.reply(202, public_job(store.get(job["id"])))

    def file(self, file, content_type, private=False):
        if not file.is_file():
            raise APIError(404, "文件不存在。")
        size = file.stat().st_size
        start, end, code = 0, size - 1, 200
        ranges = self.headers.get("Range")
        if ranges:
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", ranges)
            if not match or not any(match.groups()) or not size:
                return self.reply(416, {"error": "无效的视频范围。"}, {"Content-Range": f"bytes */{size}"})
            if not match[1]:
                start = max(0, size - int(match[2]))
            else:
                start = int(match[1])
                if match[2]:
                    end = min(end, int(match[2]))
            if start > end or start >= size:
                return self.reply(416, {"error": "无效的视频范围。"}, {"Content-Range": f"bytes */{size}"})
            code = 206
        self.send_response(code)
        self.send_header("Content-Type", content_type + ("; charset=utf-8" if content_type.startswith("text/") else ""))
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "private, no-store" if private else ("no-store" if content_type == "text/html" else "no-cache"))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("X-Frame-Options", "DENY")
        if code == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if self.command == "HEAD":
            return
        with file.open("rb") as stream:
            stream.seek(start)
            remaining = end - start + 1
            while remaining > 0:
                chunk = stream.read(min(256 * 1024, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)


def main():
    server = Server(("127.0.0.1", int(os.environ.get("PORT", "8765"))),
                    os.environ["DAYDREAMER_WEB_DATA"], os.environ["DAYDREAMER_WEB_ROOT"],
                    os.environ["DAYDREAMER_PROJECT"], os.environ["DAYDREAMER_ACCESS_CODE"],
                    os.environ.get("DAYDREAMER_UPLOAD_TOKEN", ""))
    # A killed API process cannot finish an upload. Keep completed sources/jobs.
    for job in server.store.list():
        if job["state"] == "uploading":
            server.store.upload_failed(job["id"])
    server.serve_forever()


if __name__ == "__main__":
    main()
