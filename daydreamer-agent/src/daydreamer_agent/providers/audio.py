import math
from pathlib import Path
import shutil

from daydreamer_agent.domain.errors import ValidationError
from daydreamer_agent.storage.files import file_hash, read_json, write_json


def stage_audio(manifest_path, run_path, duration, media):
    """Local asset adapter. No assumptions about a future audio generation API."""
    manifest_path = Path(manifest_path).resolve()
    manifest = read_json(manifest_path)
    if not isinstance(manifest, dict) or manifest.get("contains_speech") is not False:
        raise ValidationError("音频清单需声明 contains_speech: false；此声明不是自动内容检测。")
    if not isinstance(manifest.get("music"), dict) or not isinstance(manifest.get("sfx"), list):
        raise ValidationError("音频清单需要 music 对象和 sfx 数组。")
    tracks = [("music", manifest["music"], 0)] + [(f"sfx-{i:03}", item, item.get("at_seconds")) for i, item in enumerate(manifest["sfx"]) if isinstance(item, dict)]
    if len(tracks) != 1 + len(manifest["sfx"]) or len(tracks) > 129:
        raise ValidationError("音效条目格式错误或数量超过 128。")
    normalized = []
    # Validate every source before copying any assets.
    for name, track, at in tracks:
        if not isinstance(track.get("path"), str) or not track["path"].strip():
            raise ValidationError("每条音轨必须提供本地 path。")
        source = (manifest_path.parent / track["path"]).resolve()
        if not source.is_file():
            raise ValidationError("音频文件不存在：" + source.name)
        gain = track.get("gain", 0.3 if name == "music" else 0.7)
        if type(at) not in (int, float) or not math.isfinite(at) or not 0 <= at < duration:
            raise ValidationError("音效 at_seconds 必须处于成片时间范围内。")
        if type(gain) not in (int, float) or not math.isfinite(gain) or not 0 <= gain <= 2:
            raise ValidationError("音轨 gain 必须为 0–2 的数值。")
        media.check_audio(source)
        normalized.append({"name": name, "source": source, "at_seconds": at, "gain": gain})
    saved = []
    for track in normalized:
        target = Path(run_path) / "audio" / "inputs" / (track["name"] + track["source"].suffix)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.resolve() != track["source"]:
            temporary = target.with_suffix(target.suffix + ".tmp")
            shutil.copyfile(track["source"], temporary)
            temporary.replace(target)
        saved.append({"path": str(target.relative_to(run_path)), "name": track["name"], "at_seconds": track["at_seconds"], "gain": track["gain"], "sha256": file_hash(target)})
    write_json(Path(run_path) / "audio/manifest.json", {"contains_speech": False, "content_verified": False, "tracks": saved})
    return saved
