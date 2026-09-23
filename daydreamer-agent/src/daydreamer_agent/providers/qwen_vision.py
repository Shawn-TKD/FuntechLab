import base64
import json
from pathlib import Path

from daydreamer_agent.domain.errors import ProviderError, ValidationError
from daydreamer_agent.providers.http import JsonHTTP

# Keep the complete data URI below the documented 10 MB video limit.
MAX_VIDEO_BYTES = 6_750_000
OUTPUT_CONTRACT = """\n接口补充约束：关键画面时间必须满足0 <= timestamp_seconds < context.duration_seconds；动作区间end_seconds可以等于duration_seconds。
修正时根据repair.field与repair.expected核对视频，保留上一版其他有效内容，返回完整status、issues、card；不得把数组条目写成{}、省略号或同上，也不能只返回修改片段。没有依据时不要猜测时间或事实。"""


class QwenVision:
    def __init__(self, credentials, model="qwen3.8-max", transport=None):
        self.model = model
        self.url = credentials.openai_base + "/chat/completions"
        self.http = transport or JsonHTTP(credentials.api_key, timeout=300)

    def generate(self, instruction, context, schema, *, video=None, fps=2):
        content = []
        if video is not None:
            path = Path(video)
            if not 0 < path.stat().st_size <= MAX_VIDEO_BYTES:
                raise ValidationError("理解用视频超过本地传输上限，需要重新压缩。")
            encoded = base64.b64encode(path.read_bytes()).decode("ascii")
            content.append({"type": "video_url", "video_url": {"url": "data:video/mp4;base64," + encoded}, "fps": fps})
        content.append({"type": "text", "text": json.dumps(context, ensure_ascii=False, allow_nan=False)})
        result = self.http.request("POST", self.url, {
            "model": self.model, "messages": [{"role": "system", "content": instruction + OUTPUT_CONTRACT}, {"role": "user", "content": content}],
            "enable_thinking": False, "stream": False, "max_tokens": 16000,
            "response_format": {"type": "json_schema", "json_schema": {"name": "life_event_card", "strict": True, "schema": schema}},
        })
        try:
            choice = result["choices"][0]
            if not isinstance(choice["message"].get("content"), str):
                raise ValueError()
            return {"content": choice["message"]["content"], "finish_reason": choice.get("finish_reason"), "usage": result.get("usage", {})}
        except (KeyError, IndexError, TypeError, ValueError):
            raise ProviderError("视频理解接口没有返回文本结果；可恢复该提取任务。") from None
