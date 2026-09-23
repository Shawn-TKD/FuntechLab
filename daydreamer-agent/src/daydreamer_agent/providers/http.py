import json
import os
from pathlib import Path
import re
import socket
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from daydreamer_agent.domain.errors import ProviderError


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class JsonHTTP:
    def __init__(self, api_key, *, timeout=180, opener=None):
        self._key = api_key
        self.timeout = timeout
        self.opener = opener or build_opener(NoRedirect())

    def request(self, method, url, payload=None, *, async_video=False):
        headers = {"Authorization": "Bearer " + self._key, "Content-Type": "application/json"}
        if async_video:
            headers["X-DashScope-Async"] = "enable"
        data = None if payload is None else json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        attempts = 3 if method == "GET" else 1
        for attempt in range(attempts):
            try:
                with self.opener.open(Request(url, data=data, headers=headers, method=method), timeout=self.timeout) as response:
                    raw = response.read(8 * 1024 * 1024 + 1)
                    if len(raw) > 8 * 1024 * 1024:
                        raise ValueError("response too large")
                    result = json.loads(raw)
                    if not isinstance(result, dict):
                        raise ValueError("object required")
                    # Never persist a credential even if an upstream response echoes it.
                    return json.loads(json.dumps(result).replace(self._key, "[REDACTED]"))
            except HTTPError as exc:
                retryable = exc.code == 429 or exc.code >= 500
                code = ""
                try:
                    body = json.loads(exc.read(16384))
                    code = str(body.get("code") or body.get("error", {}).get("code") or "")
                except (ValueError, AttributeError, TypeError):
                    pass
                code = re.sub(r"[^A-Za-z0-9_.-]", "", code.replace(self._key, "REDACTED"))[:80]
                error = ProviderError(f"百炼请求失败：HTTP {exc.code} {code}", retryable=retryable,
                                      uncertain=method == "POST" and exc.code >= 500)
            except (OSError, URLError, ValueError) as exc:
                reason = exc.reason if isinstance(exc, URLError) else exc
                if isinstance(reason, (TimeoutError, socket.timeout)):
                    message = f"服务请求超时（等待上限 {self.timeout} 秒），未收到完整响应。"
                elif isinstance(exc, ValueError):
                    message = "服务返回的响应不是有效JSON对象或超过大小限制。"
                else:
                    # Do not print raw exception text: proxies can embed credentials/URLs.
                    message = f"服务连接失败（{type(reason).__name__}），请检查网络后恢复。"
                error = ProviderError(message, retryable=True, uncertain=method == "POST")
            if attempt + 1 == attempts or not error.retryable:
                raise error from None
            time.sleep(2 ** attempt)


def download(url, target, *, opener=None):
    """Download only provider OSS assets, with no model credentials attached."""
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    if parsed.scheme != "https" or not re.fullmatch(r"[a-z0-9.-]+\.aliyuncs\.com", host) or parsed.username or parsed.password or parsed.port not in (None, 443):
        raise ProviderError("视频结果地址不是支持的阿里云 HTTPS 素材地址。")
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_suffix(".download")
    opener = opener or build_opener(NoRedirect())
    try:
        with opener.open(Request(url), timeout=90) as response, partial.open("wb") as stream:
            count = 0
            while chunk := response.read(1024 * 1024):
                count += len(chunk)
                if count > 2 * 1024 ** 3:
                    raise ProviderError("单镜视频超过 2 GB 下载上限。")
                stream.write(chunk)
            stream.flush()
            os.fsync(stream.fileno())
        if count == 0:
            raise ProviderError("下载结果为空。")
        os.replace(partial, target)
    except (OSError, URLError) as exc:
        raise ProviderError("视频下载失败；保留远端任务编号，可恢复下载。", retryable=True) from None
    finally:
        partial.unlink(missing_ok=True)
