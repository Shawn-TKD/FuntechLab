"""Capabilities used by planning, validation and video submission."""
from daydreamer_agent.domain.errors import ValidationError

WAN = "wan3.0-video-prime"
H3 = "MiniMax/MiniMax-H3"


def capabilities(model):
    if model == WAN:
        return {"minimum": 3, "resolutions": {"480P", "720P", "1080P"}, "prompt_limit": 20000, "native_audio": False}
    if model == H3:
        # The current compositor uses short-edge P resolutions. 2K needs a
        # separate output-dimension contract before it can be exposed here.
        return {"minimum": 4, "resolutions": {"768P"}, "prompt_limit": 7000, "native_audio": True}
    raise ValidationError("不支持的视频模型：" + str(model))


def minimum_duration(constraints):
    return capabilities(constraints.get("video_model", WAN))["minimum"]
