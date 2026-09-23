"""Prepare bounded, silent video chunks; never extract or save still images."""
import math
from pathlib import Path

from daydreamer_agent.domain.errors import ValidationError
from daydreamer_agent.providers.qwen_vision import MAX_VIDEO_BYTES


def inspect_video(media, source):
    info = media.probe(source)
    streams = [s for s in info.get("streams", []) if s.get("codec_type") == "video" and not s.get("disposition", {}).get("attached_pic")]
    if not streams:
        raise ValidationError("所选文件没有可用视频画面。")
    stream = streams[0]
    try:
        duration = float(stream.get("duration") or info["format"]["duration"])
        width, height = int(stream["width"]), int(stream["height"])
        if not math.isfinite(duration) or duration < 2 or min(width, height) < 16:
            raise ValueError()
    except (KeyError, ValueError, TypeError):
        raise ValidationError("需要至少2秒、尺寸有效且时长可读取的视频。") from None
    return {"duration_seconds": duration, "width": width, "height": height,
            "has_audio": any(s.get("codec_type") == "audio" for s in info.get("streams", [])),
            "stream_index": stream["index"], "recorded_at": None,
            "audio_review_status": "not_analyzed"}


def chunk_ranges(duration, seconds):
    # Equal intervals avoid a final chunk shorter than the model's 2-second minimum.
    count = math.ceil(duration / seconds)
    return [(duration * i / count, duration * (i + 1) / count) for i in range(count)]


def prepare_chunk(media, source, target, material, start, end):
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_suffix(".part.mp4")
    duration = end - start
    # Do not speed up or trim black sections: seconds must map back to the source.
    try:
        media.execute([
            media.ffmpeg, "-nostdin", "-y", "-v", "error", "-protocol_whitelist", "file,pipe",
            "-i", str(source), "-ss", str(start), "-t", str(duration), "-map", "0:" + str(material["stream_index"]),
            "-vf", "setpts=PTS-STARTPTS,scale=640:640:force_original_aspect_ratio=decrease:force_divisible_by=2,setsar=1",
            "-an", "-sn", "-dn", "-map_metadata", "-1", "-map_chapters", "-1",
            "-c:v", "libx264", "-preset", "fast", "-crf", "26", "-maxrate", "550k", "-bufsize", "1100k",
            "-pix_fmt", "yuv420p", "-r", "12", "-threads", "2", "-movflags", "+faststart", str(partial),
        ])
        info = media.probe(partial)
        videos = [s for s in info.get("streams", []) if s.get("codec_type") == "video"]
        actual = float(videos[0].get("duration") or info["format"]["duration"]) if videos else 0
        if (any(s.get("codec_type") == "audio" for s in info.get("streams", [])) or not math.isfinite(actual)
                or abs(actual - duration) > 0.25 or not 0 < partial.stat().st_size <= MAX_VIDEO_BYTES):
            raise ValidationError("理解用视频未通过时长、无音轨或大小检查。")
        partial.replace(target)
    finally:
        partial.unlink(missing_ok=True)


def extraction_settings(config):
    section = config.get("extraction", {})
    settings = {"model": section.get("model", "qwen3.8-max"),
                "chunk_seconds": section.get("chunk_seconds", 60), "fps": section.get("fps", 2),
                "max_duration_seconds": section.get("max_duration_seconds", 1800)}
    if (type(settings["chunk_seconds"]) is not int or not 2 <= settings["chunk_seconds"] <= 60
            or type(settings["max_duration_seconds"]) is not int or not 2 <= settings["max_duration_seconds"] <= 7200
            or type(settings["fps"]) not in (int, float) or not 0.1 <= settings["fps"] <= 10
            or not isinstance(settings["model"], str) or not settings["model"].strip()):
        raise ValidationError("视频提取配置无效：分段2–60秒，最大时长2–7200秒，fps为0.1–10。")
    return settings
