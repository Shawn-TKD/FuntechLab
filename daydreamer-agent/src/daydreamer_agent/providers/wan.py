from urllib.parse import quote
import base64
from copy import deepcopy
from pathlib import Path
import struct

from daydreamer_agent.domain.errors import ProviderError, ValidationError
from daydreamer_agent.providers.http import JsonHTTP, download
from daydreamer_agent.providers.video_models import H3, capabilities
from daydreamer_agent.providers.temporary_upload import upload_png


MODEL = "wan3.0-video-prime"


def prepare_submission(request, first_frame=None, *, require_first_frame=False):
    payload = deepcopy(request)
    capabilities(payload.get("model"))
    h3 = payload["model"] == H3
    if require_first_frame and first_frame is None:
        raise ValidationError("图生视频必须有已生成的前段末帧。")
    if h3:
        payload["parameters"].pop("audio", None)
        payload["parameters"].pop("prompt_extend", None)
    else:
        payload["parameters"].update(audio=False, prompt_extend=False)
    if first_frame is not None:
        image = Path(first_frame).read_bytes()
        if not 26 <= len(image) <= 20 * 1024 * 1024 or image[:8] != b"\x89PNG\r\n\x1a\n":
            raise ValidationError("续接首帧必须是有效 PNG，且不能超过20MB。")
        width, height = struct.unpack(">II", image[16:24])
        if h3 and (min(width, height) < 256 or max(width, height) > 5760 or not 0.4 <= width / height <= 2.5):
            raise ValidationError("MiniMax H3 首帧尺寸或宽高比超出模型限制。")
        if min(width, height) < 240 or max(width, height) > 8000 or not 1/8 <= width / height <= 8:
            raise ValidationError("续接首帧尺寸或宽高比超出模型限制。")
        if image[25] not in {0, 2}:
            raise ValidationError("续接首帧不能包含透明通道。")
        offset = 8
        while offset + 12 <= len(image):
            length = struct.unpack(">I", image[offset:offset + 4])[0]
            if offset + length + 12 > len(image):
                raise ValidationError("续接首帧 PNG 数据不完整。")
            if image[offset + 4:offset + 8] == b"tRNS":
                raise ValidationError("续接首帧不能包含透明通道。")
            offset += length + 12
        payload["parameters"]["ratio"] = "adaptive"
        payload["input"]["media"] = [{"type": "first_frame", "url": "data:image/png;base64," + base64.b64encode(image).decode("ascii")}]
    return payload


class BailianVideo:
    def __init__(self, credentials, transport=None):
        self.base = credentials.api_base
        self.http = transport or JsonHTTP(credentials.api_key)

    def submit(self, request):
        capabilities(request.get("model"))
        request = deepcopy(request)
        if request["model"] == H3:
            from daydreamer_agent.prompts.video_compiler import validate_video_request
            checked = deepcopy(request)
            if checked["parameters"].get("ratio") == "adaptive" and checked["input"].get("media"):
                checked["parameters"]["ratio"] = "16:9"
            validate_video_request(checked)
            for item in request["input"].get("media", []):
                prefix = "data:image/png;base64,"
                if item.get("url", "").startswith(prefix):
                    image = base64.b64decode(item["url"][len(prefix):], validate=True)
                    item["url"] = upload_png(self.http, self.base, H3, image)
        response = self.http.request("POST", self.base + "/services/aigc/video-generation/video-synthesis",
                                     request, async_video=True)
        task_id = response.get("output", {}).get("task_id")
        if not isinstance(task_id, str) or not task_id:
            raise ProviderError("视频提交未返回任务编号，需要核对平台记录。", uncertain=True)
        return task_id

    def query(self, task_id):
        result = self.http.request("GET", self.base + "/tasks/" + quote(task_id, safe=""))
        output = result.get("output", {})
        if output.get("task_status") not in {"PENDING", "RUNNING", "SUCCEEDED", "FAILED", "CANCELED", "UNKNOWN"}:
            raise ProviderError("视频查询未返回有效任务状态。", retryable=True)
        return output

    def download(self, url, target):
        download(url, target)


# Retain the old import for callers resuming saved Wan jobs.
WanVideo = BailianVideo
