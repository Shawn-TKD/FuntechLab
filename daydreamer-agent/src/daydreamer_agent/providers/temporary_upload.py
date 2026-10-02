"""Bailian temporary, model-bound OSS upload; no API key is sent to OSS."""
import re
import uuid
from urllib.parse import quote, urlsplit
from urllib.request import Request, build_opener

from daydreamer_agent.domain.errors import ProviderError
from daydreamer_agent.providers.http import NoRedirect


def upload_png(http, base, model, image, *, opener=None):
    policy = http.request("GET", base + "/uploads?action=getPolicy&model=" + quote(model, safe="")).get("data", {})
    required = ("upload_host", "upload_dir", "oss_access_key_id", "signature", "policy", "x_oss_object_acl", "x_oss_forbid_overwrite")
    if not all(isinstance(policy.get(key), str) and policy[key] for key in required):
        raise ProviderError("首帧上传凭证不完整，尚未提交视频。")
    host = urlsplit(policy["upload_host"])
    if (host.scheme != "https" or not re.fullmatch(r"[a-z0-9-]+\.oss-[a-z0-9-]+\.aliyuncs\.com", host.hostname or "")
            or host.username or host.password or host.port not in (None, 443) or host.query or host.fragment):
        raise ProviderError("首帧上传地址不是支持的阿里云 OSS HTTPS 地址。")
    name = uuid.uuid4().hex + ".png"
    key = policy["upload_dir"].rstrip("/") + "/" + name
    fields = {"OSSAccessKeyId": policy["oss_access_key_id"], "Signature": policy["signature"], "policy": policy["policy"],
              "x-oss-object-acl": policy["x_oss_object_acl"], "x-oss-forbid-overwrite": policy["x_oss_forbid_overwrite"],
              "key": key, "success_action_status": "200"}
    boundary = uuid.uuid4().hex
    body = b"".join((f'--{boundary}\r\nContent-Disposition: form-data; name="{field}"\r\n\r\n{value}\r\n').encode()
                    for field, value in fields.items())
    body += (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{name}"\r\nContent-Type: image/png\r\n\r\n').encode()
    body += image + f"\r\n--{boundary}--\r\n".encode()
    request = Request(policy["upload_host"], data=body, headers={"Content-Type": "multipart/form-data; boundary=" + boundary}, method="POST")
    try:
        with (opener or build_opener(NoRedirect())).open(request, timeout=120) as response:
            if response.status != 200:
                raise OSError("upload status")
    except OSError:
        raise ProviderError("首帧上传失败，尚未提交视频，可重试当前镜头。", retryable=True) from None
    return "oss://" + key
