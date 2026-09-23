import csv
from dataclasses import dataclass, field
import os
from pathlib import Path
import re
import tomllib
from urllib.parse import urlsplit

from daydreamer_agent.domain.errors import ValidationError


def validate_endpoint(value):
    parsed = urlsplit(value)
    host = parsed.hostname or ""
    allowed = host in {"dashscope.aliyuncs.com", "dashscope-intl.aliyuncs.com", "dashscope-us.aliyuncs.com", "cn-hongkong.dashscope.aliyuncs.com"}
    allowed |= bool(re.fullmatch(r"[a-z0-9-]+\.(cn-beijing|ap-southeast-1|us-east-1|eu-central-1|ap-northeast-1|cn-hongkong)\.maas\.aliyuncs\.com", host))
    if not allowed or parsed.scheme != "https" or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.port not in (None, 443):
        raise ValidationError("模型端点必须是百炼官方 HTTPS 地址。")
    return value.rstrip("/")


def read_env_file(root):
    values = {}
    env_file = Path(root) / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip().strip("\"'")
    return values


@dataclass(repr=False)
class Credentials:
    api_key: str = field(repr=False)
    openai_base: str
    api_base: str

    @classmethod
    def load(cls, root, csv_path=None):
        values = read_env_file(root)
        names = ("DASHSCOPE_API_KEY", "DASHSCOPE_OPENAI_BASE_URL", "DASHSCOPE_API_BASE_URL")
        if csv_path:
            with Path(csv_path).open(encoding="utf-8-sig", newline="") as stream:
                rows = dict(row[:2] for row in csv.reader(stream) if len(row) >= 2)
            values.update(dict(zip(names, (rows.get("apiKey", ""), rows.get("openAiCompatible", ""), rows.get("dashScope", "")))))
        for name in names:
            if os.environ.get(name):
                values[name] = os.environ[name]
        if not all(values.get(name, "").strip() for name in names):
            raise ValidationError("请配置三个 DASHSCOPE 环境变量，或用 --credentials-csv 指定密钥 CSV。")
        secret = values[names[0]].strip()
        if "\n" in secret or "\r" in secret:
            raise ValidationError("API Key 格式错误。")
        openai_base, api_base = (validate_endpoint(values[n].strip()) for n in names[1:])
        if not openai_base.endswith("/compatible-mode/v1") or not api_base.endswith("/api/v1"):
            raise ValidationError("端点路径必须分别以 /compatible-mode/v1 和 /api/v1 结尾。")
        if urlsplit(openai_base).hostname != urlsplit(api_base).hostname:
            raise ValidationError("两个服务端点必须属于同一业务空间及地域。")
        return cls(secret, openai_base, api_base)


def load_settings(root):
    with (Path(root) / "config/default.toml").open("rb") as stream:
        config = tomllib.load(stream)
    if config["checks"]["media_content"]:
        raise ValidationError("当前版本未实现媒体内容检查。")
    video_retry_policy(config)
    timeout = config["story"].get("request_timeout_seconds", 600)
    if type(timeout) is not int or not 30 <= timeout <= 1800:
        raise ValidationError("story.request_timeout_seconds必须为30至1800秒的整数。")
    restarts = config["story"].get("max_creation_restarts", 2)
    if type(restarts) is not int or not 0 <= restarts <= 5:
        raise ValidationError("story.max_creation_restarts必须为0至5的整数。")
    from daydreamer_agent.story.creativity import policy
    policy(config)
    return config


def video_retry_policy(config):
    video = config.get("video", {})
    timeout = video.get("generation_timeout_seconds", 300)
    retries = video.get("max_timeout_resubmissions", 1)
    if type(timeout) is not int or timeout <= 0:
        raise ValidationError("generation_timeout_seconds 必须是正整数。")
    if type(retries) is not int or not 0 <= retries <= 5:
        raise ValidationError("max_timeout_resubmissions 必须为0–5，0表示关闭自动重提。")
    return timeout, retries
